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

log = logging.getLogger("paperead.store")

ATTACHMENT_LIMIT = 20 * 1024 * 1024
_ATTACHMENT_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _attachment_name(name: str) -> str:
    base = Path(name or "attachment").name
    base = _ATTACHMENT_BAD.sub("_", base).strip(" .") or "attachment"
    stem, suffix = Path(base).stem[:80], Path(base).suffix[:16]
    return f"{stem or 'attachment'}{suffix}"


class FolderNameError(ValueError):
    """文件夹名称非法。"""


class FolderExistsError(ValueError):
    """同名文件夹已存在。"""

# 原版 PDF 页图的分辨率范围：太小看不清公式，太大传输慢。
PAGE_WIDTH_MIN = 480
PAGE_WIDTH_MAX = 1600


class LibraryStore:
    def __init__(self, root: Path):
        self.root = root
        self.papers_dir = root / "papers"
        self.index = root / "library.json"
        self.folders_path = root / "folders.json"
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

    def list(self, include_project: bool = False) -> list[dict[str, Any]]:
        """文献库列表。

        带 ``project_id`` 的行属于研究项目自己的论文文件夹，默认不出现在
        全局文献库里；只有检索索引等内部场景才需要 ``include_project=True``。
        """
        rows = [
            r for r in self._read()
            if include_project or not r.get("project_id")
        ]
        rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return rows

    def list_project(self, project_id: str) -> list[dict[str, Any]]:
        """某个研究项目自己的论文（按创建时间倒序）。"""
        rows = [r for r in self._read() if r.get("project_id") == project_id]
        rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return rows

    def get(self, paper_id: str) -> dict[str, Any] | None:
        return next((r for r in self._read() if r["id"] == paper_id), None)

    def find_by_source(
        self, source_id: str, project_id: str | None = None
    ) -> dict[str, Any] | None:
        """按外部题录 id（如 arxiv:2401.00001）查重。

        ``project_id`` 为空时只查全局文献库；给定时只在该项目范围内查重，
        避免项目论文和全局库互相“命中”。
        """
        if not source_id:
            return None
        scope = project_id or None
        return next(
            (
                r for r in self._read()
                if r.get("source_id") == source_id
                and (r.get("project_id") or None) == scope
            ),
            None,
        )

    def update(self, paper_id: str, **patch: Any) -> dict[str, Any] | None:
        with self._lock:
            rows = self._read()
            for r in rows:
                if r["id"] == paper_id:
                    r.update(patch)
                    self._write(rows)
                    return r
        return None

    # ── 文件夹 ──────────────────────────────────────────

    def _read_folders(self) -> list[dict[str, Any]]:
        if not self.folders_path.exists():
            return []
        try:
            payload = json.loads(self.folders_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(payload, dict):
            rows = payload.get("folders") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        return [
            row
            for row in rows
            if isinstance(row, dict) and row.get("id") and row.get("name")
        ]

    def _write_folders(self, folders: list[dict[str, Any]]) -> None:
        self.folders_path.write_text(
            json.dumps(
                {"version": 1, "folders": folders},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _clean_folder_name(name: str) -> str:
        value = re.sub(r"\s+", " ", (name or "").strip())
        if not value:
            raise FolderNameError("文件夹名称不能为空")
        if len(value) > 40:
            raise FolderNameError("文件夹名称不能超过 40 个字")
        return value

    @staticmethod
    def _folder_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            if row.get("project_id"):
                continue
            folder_id = row.get("folder_id")
            if folder_id:
                counts[str(folder_id)] = counts.get(str(folder_id), 0) + 1
        return counts

    def list_folders(self) -> list[dict[str, Any]]:
        with self._lock:
            folders = self._read_folders()
            counts = self._folder_counts(self._read())
        folders.sort(key=lambda row: (row.get("created_at", 0), row["id"]))
        return [
            {**row, "paper_count": counts.get(row["id"], 0)}
            for row in folders
        ]

    def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        return next(
            (row for row in self._read_folders() if row["id"] == folder_id),
            None,
        )

    def create_folder(
        self,
        name: str,
        *,
        zotero_key: str = "",
        zotero_name: str = "",
    ) -> dict[str, Any]:
        cleaned = self._clean_folder_name(name)
        with self._lock:
            folders = self._read_folders()
            if any(row["name"].casefold() == cleaned.casefold() for row in folders):
                raise FolderExistsError("文件夹名称已存在")
            folder = {
                "id": f"f_{uuid.uuid4().hex[:12]}",
                "name": cleaned,
                "created_at": time.time(),
            }
            if zotero_key:
                folder["zotero_key"] = zotero_key
                folder["zotero_name"] = zotero_name or cleaned
            folders.append(folder)
            self._write_folders(folders)
        return {**folder, "paper_count": 0}

    def update_folder(
        self, folder_id: str, **patch: Any
    ) -> dict[str, Any] | None:
        """更新文件夹的同步元数据（Zotero key / 名称快照等）。"""
        with self._lock:
            folders = self._read_folders()
            target = next(
                (row for row in folders if row["id"] == folder_id),
                None,
            )
            if target is None:
                return None
            target.update(patch)
            self._write_folders(folders)
        return target

    def find_folder_by_zotero(self, zotero_key: str) -> dict[str, Any] | None:
        if not zotero_key:
            return None
        return next(
            (
                row
                for row in self._read_folders()
                if row.get("zotero_key") == zotero_key
            ),
            None,
        )

    def rename_folder(self, folder_id: str, name: str) -> dict[str, Any] | None:
        cleaned = self._clean_folder_name(name)
        with self._lock:
            folders = self._read_folders()
            target = next(
                (row for row in folders if row["id"] == folder_id),
                None,
            )
            if target is None:
                return None
            if any(
                row["id"] != folder_id
                and row["name"].casefold() == cleaned.casefold()
                for row in folders
            ):
                raise FolderExistsError("文件夹名称已存在")
            target["name"] = cleaned
            self._write_folders(folders)
            counts = self._folder_counts(self._read())
        return {**target, "paper_count": counts.get(folder_id, 0)}

    def delete_folder(self, folder_id: str) -> dict[str, Any] | None:
        with self._lock:
            folders = self._read_folders()
            if not any(row["id"] == folder_id for row in folders):
                return None
            rows = self._read()
            moved = 0
            for row in rows:
                if row.get("folder_id") == folder_id:
                    row.pop("folder_id", None)
                    moved += 1
            self._write(rows)
            self._write_folders(
                [row for row in folders if row["id"] != folder_id]
            )
        return {"ok": True, "moved": moved}

    def move_paper(
        self, paper_id: str, folder_id: str | None
    ) -> dict[str, Any] | None:
        with self._lock:
            rows = self._read()
            paper = next(
                (row for row in rows if row["id"] == paper_id),
                None,
            )
            if paper is None:
                return None
            if folder_id and not any(
                row["id"] == folder_id for row in self._read_folders()
            ):
                raise LookupError("文件夹不存在")
            if folder_id:
                paper["folder_id"] = folder_id
            else:
                paper.pop("folder_id", None)
            self._write(rows)
        return paper

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

    # ── 问答附件 ────────────────────────────────────────

    def attachments_dir(self, paper_id: str) -> Path:
        return self.root / "attachments" / paper_id

    def _attachment_manifest_path(self, paper_id: str) -> Path:
        return self.attachments_dir(paper_id) / "paper.json"

    def _read_attachment_manifest(self, paper_id: str) -> dict[str, Any]:
        path = self._attachment_manifest_path(paper_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_attachment_manifest(
        self, paper_id: str, manifest: dict[str, Any]
    ) -> None:
        folder = self.attachments_dir(paper_id)
        folder.mkdir(parents=True, exist_ok=True)
        self._attachment_manifest_path(paper_id).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def save_attachment(
        self,
        paper_id: str,
        filename: str,
        data: bytes,
        title: str = "",
    ) -> dict[str, Any]:
        folder = self.attachments_dir(paper_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / _attachment_name(filename)
        stem, suffix = target.stem, target.suffix
        index = 1
        while target.exists():
            target = folder / f"{stem}-{index}{suffix}"
            index += 1
        target.write_bytes(data)
        now = time.time()
        manifest = self._read_attachment_manifest(paper_id)
        manifest["paperId"] = paper_id
        manifest["title"] = title or manifest.get("title") or ""
        manifest["updatedAt"] = now
        files = manifest.get("files")
        if not isinstance(files, dict):
            files = {}
        files[target.name] = {
            "size": len(data),
            "suffix": suffix.lower(),
            "createdAt": now,
        }
        manifest["files"] = files
        self._write_attachment_manifest(paper_id, manifest)
        return {
            "name": target.name,
            "size": len(data),
            "suffix": suffix.lower(),
            "createdAt": now,
            "url": f"/api/papers/{paper_id}/attachments/{target.name}",
        }

    def annotate_attachment(
        self, paper_id: str, name: str, **patch: Any
    ) -> None:
        manifest = self._read_attachment_manifest(paper_id)
        files = manifest.get("files")
        if not isinstance(files, dict):
            return
        item = files.get(name)
        if not isinstance(item, dict):
            return
        item.update(patch)
        manifest["updatedAt"] = time.time()
        self._write_attachment_manifest(paper_id, manifest)

    def list_attachments(self, paper_id: str) -> list[dict[str, Any]]:
        folder = self.attachments_dir(paper_id)
        if not folder.is_dir():
            return []
        manifest = self._read_attachment_manifest(paper_id)
        files = manifest.get("files")
        files = files if isinstance(files, dict) else {}
        items: list[dict[str, Any]] = []
        for path in folder.iterdir():
            if not path.is_file() or path.name == "paper.json":
                continue
            meta = files.get(path.name)
            meta = meta if isinstance(meta, dict) else {}
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "name": path.name,
                    "size": int(meta.get("size") or stat.st_size),
                    "suffix": path.suffix.lower(),
                    "createdAt": meta.get("createdAt") or stat.st_mtime,
                    "textChars": int(meta.get("textChars") or 0),
                    "url": f"/api/papers/{paper_id}/attachments/{path.name}",
                }
            )
        items.sort(key=lambda item: item["createdAt"], reverse=True)
        return items

    def delete_attachment(self, paper_id: str, name: str) -> bool:
        path = self.attachment_path(paper_id, name)
        if path is None:
            return False
        path.unlink(missing_ok=True)
        manifest = self._read_attachment_manifest(paper_id)
        files = manifest.get("files")
        if isinstance(files, dict):
            files.pop(path.name, None)
            manifest["updatedAt"] = time.time()
            self._write_attachment_manifest(paper_id, manifest)
        return True

    def attachment_path(self, paper_id: str, name: str) -> Path | None:
        folder = self.attachments_dir(paper_id).resolve()
        candidate = (folder / _attachment_name(name)).resolve()
        if folder not in candidate.parents:
            return None
        return candidate if candidate.is_file() else None

    def remove(self, paper_id: str) -> bool:
        with self._lock:
            rows = self._read()
            kept = [r for r in rows if r["id"] != paper_id]
            if len(kept) == len(rows):
                return False
            self._write(kept)
        for suffix in (
            ".pdf",
            ".thumb.png",
            ".blocks.json",
            ".annotations.json",
            ".highlights.json",
        ):
            path = self.papers_dir / f"{paper_id}{suffix}"
            if path.exists():
                path.unlink(missing_ok=True)
        pages = self.page_dir(paper_id)
        if pages.exists():
            shutil.rmtree(pages, ignore_errors=True)
        attachments = self.attachments_dir(paper_id)
        if attachments.exists():
            shutil.rmtree(attachments, ignore_errors=True)
        return True

    # ── 标注 / 笔记 ─────────────────────────────────────

    def annotations_path(self, paper_id: str) -> Path:
        return self.papers_dir / f"{paper_id}.annotations.json"

    def list_annotations(self, paper_id: str) -> list[dict[str, Any]]:
        path = self.annotations_path(paper_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("标注文件损坏，忽略：%s", path)
            return []
        rows = data if isinstance(data, list) else []
        # 旧数据没有 kind：默认按荧光笔高亮处理
        for row in rows:
            if isinstance(row, dict):
                row.setdefault("kind", "highlight")
        return rows

    def _write_annotations(
        self, paper_id: str, rows: list[dict[str, Any]]
    ) -> None:
        self.annotations_path(paper_id).write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def add_annotation(
        self, paper_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        now = time.time()
        row = {
            "id": f"a_{uuid.uuid4().hex[:12]}",
            "block_id": data["block_id"],
            "source": data.get("source") or "en",
            "start": int(data.get("start") or 0),
            "end": int(data.get("end") or 0),
            "quote": data.get("quote") or "",
            "color": data.get("color") or "yellow",
            "kind": data.get("kind") or "highlight",
            "note": data.get("note") or "",
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            rows = self.list_annotations(paper_id)
            rows.append(row)
            self._write_annotations(paper_id, rows)
        return row

    def update_annotation(
        self, paper_id: str, annotation_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {"color", "note", "quote", "start", "end"}
        with self._lock:
            rows = self.list_annotations(paper_id)
            for row in rows:
                if row.get("id") != annotation_id:
                    continue
                for key, value in patch.items():
                    if key in allowed and value is not None:
                        row[key] = value
                row["updated_at"] = time.time()
                self._write_annotations(paper_id, rows)
                return row
        return None

    def delete_annotation(self, paper_id: str, annotation_id: str) -> bool:
        with self._lock:
            rows = self.list_annotations(paper_id)
            kept = [row for row in rows if row.get("id") != annotation_id]
            if len(kept) == len(rows):
                return False
            self._write_annotations(paper_id, kept)
        return True

    # ── 自动阅读重点（系统高亮） ────────────────────────

    def highlights_path(self, paper_id: str) -> Path:
        return self.papers_dir / f"{paper_id}.highlights.json"

    def read_highlights(self, paper_id: str) -> dict[str, Any]:
        """返回 {"version":1,"pages":{...},"highlights":[...]}，损坏时返回空结构。"""
        path = self.highlights_path(paper_id)
        empty = {"version": 1, "pages": {}, "highlights": []}
        if not path.exists():
            return empty
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("阅读重点文件损坏，忽略：%s", path)
            return empty
        if not isinstance(data, dict):
            return empty
        data.setdefault("version", 1)
        data.setdefault("pages", {})
        data.setdefault("highlights", [])
        return data

    def _write_highlights(self, paper_id: str, data: dict[str, Any]) -> None:
        self.highlights_path(paper_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def list_highlights(self, paper_id: str) -> list[dict[str, Any]]:
        rows = self.read_highlights(paper_id).get("highlights") or []
        return rows if isinstance(rows, list) else []

    def highlight_pages(self, paper_id: str) -> dict[str, Any]:
        pages = self.read_highlights(paper_id).get("pages") or {}
        return pages if isinstance(pages, dict) else {}

    def replace_page_highlights(
        self, paper_id: str, page: int, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """用新结果替换某一页的自动重点；没有结果也记一笔，避免重复请求。"""
        now = time.time()
        with self._lock:
            data = self.read_highlights(paper_id)
            kept = [
                row
                for row in data.get("highlights", [])
                if int(row.get("page") or 0) != int(page)
            ]
            added = []
            for row in rows:
                item = {
                    **row,
                    "id": f"h_{uuid.uuid4().hex[:12]}",
                    "page": int(page),
                    "created_at": now,
                }
                added.append(item)
            data["highlights"] = kept + added
            data.setdefault("pages", {})[str(page)] = {
                "at": now,
                "count": len(added),
            }
            self._write_highlights(paper_id, data)
        return added

    def clear_highlights(self, paper_id: str) -> None:
        path = self.highlights_path(paper_id)
        if path.exists():
            path.unlink(missing_ok=True)

    # ── 文件 ────────────────────────────────────────────

    def pdf_path(self, paper_id: str) -> Path | None:
        p = self.papers_dir / f"{paper_id}.pdf"
        return p if p.exists() else None

    def thumb_path(self, paper_id: str) -> Path | None:
        p = self.papers_dir / f"{paper_id}.thumb.png"
        return p if p.exists() else None

    def page_dir(self, paper_id: str) -> Path:
        return self.papers_dir / f"{paper_id}_pages"

    def page_sizes(self, paper_id: str) -> list[list[float]]:
        """每页 [宽, 高]（PDF 点）。前端拿它给原版 PDF 的页图占位。"""
        pdf = self.pdf_path(paper_id)
        if not pdf:
            return []
        try:
            doc = pymupdf.open(str(pdf))
        except Exception:
            return []
        try:
            return [
                [round(page.rect.width, 1), round(page.rect.height, 1)]
                for page in doc
            ]
        finally:
            doc.close()

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

    def update_block_latex(self, paper_id: str, block_id: str, latex: str) -> bool:
        """把阅读时按需识别出的 LaTeX 回写到该论文的 blocks.json。

        只改指定块，保留译文、图表等其他字段；用锁串行化，避免多个公式
        同时识别时互相覆盖。
        """
        if not block_id or not latex:
            return False
        with self._lock:
            blocks = self.read_blocks(paper_id)
            if blocks is None:
                return False
            for block in blocks:
                if block.get("id") != block_id:
                    continue
                if block.get("latex") == latex:
                    return True
                block["latex"] = latex
                self.write_blocks(paper_id, blocks)
                return True
        return False


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
