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

    def add(self, filename: str, data: bytes) -> dict[str, Any]:
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
        return True

    # ── 文件 ────────────────────────────────────────────

    def pdf_path(self, paper_id: str) -> Path | None:
        p = self.papers_dir / f"{paper_id}.pdf"
        return p if p.exists() else None

    def thumb_path(self, paper_id: str) -> Path | None:
        p = self.papers_dir / f"{paper_id}.thumb.png"
        return p if p.exists() else None

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
