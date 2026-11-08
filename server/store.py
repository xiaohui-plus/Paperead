"""文献库存储：文件落盘 + 元信息 JSON + 缩略图 + 解析。

目录结构：
    data/
      library.json          # [{id,title,authors,pages,size,status,...}]
      papers/
        {id}.pdf
        {id}.thumb.png
        {id}.blocks.json    # 解析结果（blocks）
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

import pymupdf

log = logging.getLogger("paper-reflow.store")

# 原版 PDF 页图的分辨率范围：太小看不清公式，太大传输慢。
PAGE_WIDTH_MIN = 480
PAGE_WIDTH_MAX = 1600


class LibraryStore:
    def __init__(self, root: Path):
        self.root = root
        self.papers_dir = root / "papers"
        self.index = root / "library.json"
        self.papers_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    # ── 元信息 ──────────────────────────────────────────

    def _read(self) -> list[dict[str, Any]]:
        if not self.index.exists():
            return []
        try:
            return json.loads(self.index.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.index.write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def list(self) -> list[dict[str, Any]]:
        rows = self._read()
        rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return rows

    def get(self, paper_id: str) -> dict[str, Any] | None:
        return next((r for r in self._read() if r["id"] == paper_id), None)

    def find_by_source(self, source_id: str) -> dict[str, Any] | None:
        """按外部题录 id（如 arxiv:2401.00001）查重，检索页导入时用。"""
        if not source_id:
            return None
        return next((r for r in self._read() if r.get("source_id") == source_id), None)

    def update(self, paper_id: str, **patch: Any) -> dict[str, Any] | None:
        with self._lock:
            rows = self._read()
            for r in rows:
                if r["id"] == paper_id:
                    r.update(patch)
                    self._write(rows)
                    return r
        return None

    # ── 增删 ────────────────────────────────────────────

    def add(
        self, filename: str, data: bytes, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        paper_id = f"p_{uuid.uuid4().hex[:12]}"
        pdf = self.papers_dir / f"{paper_id}.pdf"
        pdf.write_bytes(data)

        title, authors, pages = _probe_pdf(pdf)
        row = {
            "id": paper_id,
            "title": title or Path(filename).stem,
            "authors": authors,
            "filename": filename,
            "pages": pages,
            "size": len(data),
            "created_at": time.time(),
            "status": "uploaded",
            "error": "",
            "translated": 0,
            "blocks": 0,
        }
        # 检索页导入时带上来源信息：source / source_id 用于去重，标题作者兜底用
        if meta:
            for key, value in meta.items():
                if value not in (None, ""):
                    row[key] = value
        with self._lock:
            rows = self._read()
            rows.append(row)
            self._write(rows)

        try:
            render_thumbnail(pdf, self.papers_dir / f"{paper_id}.thumb.png")
        except Exception as e:  # 缩略图失败不影响主流程
            log.warning("生成缩略图失败：%s", e)
        return row

    def remove(self, paper_id: str) -> bool:
        with self._lock:
            rows = self._read()
            kept = [r for r in rows if r["id"] != paper_id]
            if len(kept) == len(rows):
                return False
            self._write(kept)
        for suffix in (".pdf", ".thumb.png", ".blocks.json"):
            path = self.papers_dir / f"{paper_id}{suffix}"
            if path.exists():
                path.unlink(missing_ok=True)
        pages = self.page_dir(paper_id)
        if pages.exists():
            shutil.rmtree(pages, ignore_errors=True)
        return True

    # ── 文件 ────────────────────────────────────────────

    def pdf_path(self, paper_id: str) -> Path | None:
        p = self.papers_dir / f"{paper_id}.pdf"
        return p if p.exists() else None

    def thumb_path(self, paper_id: str) -> Path | None:
        p = self.papers_dir / f"{paper_id}.thumb.png"
        return p if p.exists() else None

    def page_dir(self, paper_id: str) -> Path:
        return self.papers_dir / f"{paper_id}_pages"

    def render_page(self, paper_id: str, page: int, width: int = 1400) -> bytes | None:
        """把 PDF 的第 N 页（1 起）渲染成 PNG。用于阅读页「原版 PDF」视图。

        结果按「页码 + 宽度」缓存在 `{id}_pages/`：同一页只渲染一次，
        删论文时整目录一起清掉。
        """
        pdf = self.pdf_path(paper_id)
        if not pdf:
            return None
        width = max(PAGE_WIDTH_MIN, min(int(width or 1400), PAGE_WIDTH_MAX))
        cached = self.page_dir(paper_id) / f"p{page:04d}@{width}.png"
        if cached.exists():
            return cached.read_bytes()

        doc = pymupdf.open(str(pdf))
        try:
            if page < 1 or page > len(doc):
                return None
            target = doc[page - 1]
            zoom = width / target.rect.width if target.rect.width else 1.0
            pix = target.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            data = pix.tobytes("png")
        finally:
            doc.close()

        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return data

    def blocks_path(self, paper_id: str) -> Path:
        return self.papers_dir / f"{paper_id}.blocks.json"

    def read_blocks(self, paper_id: str) -> list[dict[str, Any]] | None:
        p = self.blocks_path(paper_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def write_blocks(self, paper_id: str, blocks: list[dict[str, Any]]) -> None:
        self.blocks_path(paper_id).write_text(
            json.dumps(blocks, ensure_ascii=False), encoding="utf-8"
        )


# ── PDF 探测 ────────────────────────────────────────────


def _probe_pdf(path: Path) -> tuple[str, str, int]:
    """取标题、作者、页数。标题优先用最大字号那一行（首页），其次用 PDF 元数据。"""
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return "", "", 0

    pages = len(doc)
    title, authors = "", ""
    try:
        meta = doc.metadata or {}
        title = (meta.get("title") or "").strip()
        authors = (meta.get("author") or "").strip()
    except Exception:
        pass

    if not title and pages:
        try:
            page = doc[0]
            blocks = page.get_text("dict")["blocks"]
            best, best_size = "", 0.0
            for b in blocks:
                if b.get("type") != 0:
                    continue
                for line in b.get("lines", []):
                    text = "".join(s["text"] for s in line.get("spans", [])).strip()
                    size = max((s.get("size", 0) for s in line.get("spans", [])), default=0)
                    if 20 <= len(text) <= 300 and size > best_size:
                        best, best_size = text, size
            title = best
        except Exception:
            pass

    doc.close()
    return _clean(title), _clean(authors, limit=200), pages


def _clean(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def render_thumbnail(pdf: Path, out: Path, width: int = 420) -> None:
    """首页渲染成缩略图（按宽度缩放，保持比例）。"""
    doc = pymupdf.open(str(pdf))
    try:
        page = doc[0]
        zoom = width / page.rect.width if page.rect.width else 1.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out))
    finally:
        doc.close()
