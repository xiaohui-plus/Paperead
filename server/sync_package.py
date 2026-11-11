"""离线同步包：把电脑上的论文打成单个 ``.paperead`` 文件。

包是标准 zip，内含 manifest.json、每篇论文的 blocks、PDF、缩略图、页图与
正文图片。手机通过系统文件选择器导入，完全不需要 Wi-Fi、端口或服务器。
"""

from __future__ import annotations

import json
import mimetypes
import socket
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from paperead import __version__
from server.research.files import safe_name
from server.store import LibraryStore

PAGE_WIDTH = 1000
MAX_PAGES = 500
MAX_PAPERS = 200
KEEP_EXPORTS = 5
PACKAGE_FORMAT = "paperead-sync"
PACKAGE_VERSION = 1
EXPORT_DIR_NAME = "sync-exports"


class ExportIn(BaseModel):
    ids: list[str] = Field(default_factory=list)


def _asset_files(store: LibraryStore, paper_id: str, payload: dict[str, Any]) -> dict[str, Path]:
    prefix = f"/api/papers/{paper_id}/asset/"
    base = (store.papers_dir / f"{paper_id}_assets").resolve()
    found: dict[str, Path] = {}
    for block in payload.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        url = block.get("image_path")
        if not isinstance(url, str) or not url.startswith("/api/"):
            continue
        if not url.startswith(prefix):
            continue
        relative = unquote(url[len(prefix):])
        target = (base / relative).resolve()
        if target != base and base not in target.parents:
            continue
        if target.is_file():
            found[url] = target
    return found


def _page_count(payload: dict[str, Any], row: dict[str, Any]) -> int:
    try:
        value = int(payload.get("page_count") or row.get("pages") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_PAGES))


def _write_package(store: LibraryStore, rows: list[dict[str, Any]], target: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "exportedAt": int(time.time() * 1000),
        "device": socket.gethostname(),
        "appVersion": __version__,
        "papers": [],
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
        for row in rows:
            paper_id = str(row["id"])
            blocks_path = store.blocks_path(paper_id)
            if not blocks_path.is_file():
                continue
            payload = json.loads(blocks_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                # 旧版 blocks.json 直接是块数组，包一层保持导出兼容。
                payload = {"blocks": payload}
            if not isinstance(payload, dict):
                continue

            folder = f"papers/{paper_id}"
            archive.writestr(
                f"{folder}/blocks.json",
                json.dumps(payload, ensure_ascii=False),
            )
            files: dict[str, Any] = {
                "blocks": f"{folder}/blocks.json",
                "pdf": None,
                "thumb": None,
                "pages": {},
                "assets": {},
            }

            pdf = store.pdf_path(paper_id)
            if pdf is not None and pdf.is_file():
                files["pdf"] = f"{folder}/paper.pdf"
                archive.write(pdf, files["pdf"])

            thumb = store.thumb_path(paper_id)
            if thumb is not None and thumb.is_file():
                files["thumb"] = f"{folder}/thumb.png"
                archive.write(thumb, files["thumb"])

            for page in range(1, _page_count(payload, row) + 1):
                try:
                    data = store.render_page(paper_id, page, PAGE_WIDTH)
                except Exception:  # noqa: BLE001 - 单页渲染失败不影响整个包
                    data = None
                if not data:
                    continue
                page_file = f"{folder}/pages/{page}.png"
                archive.writestr(page_file, data)
                files["pages"][str(page)] = page_file

            for url, path in _asset_files(store, paper_id, payload).items():
                suffix = path.suffix or mimetypes.guess_extension(
                    mimetypes.guess_type(path.name)[0] or ""
                ) or ".bin"
                asset_file = f"{folder}/assets/{uuid.uuid4().hex[:8]}{suffix}"
                archive.write(path, asset_file)
                files["assets"][url] = asset_file

            manifest["papers"].append(
                {
                    "id": paper_id,
                    "title": str(row.get("title") or payload.get("title") or "未命名"),
                    "authors": str(row.get("authors") or ""),
                    "pages": _page_count(payload, row),
                    "files": files,
                }
            )

        archive.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False)
        )
    return manifest


def _cleanup(exports_dir: Path) -> None:
    try:
        files = sorted(
            exports_dir.glob("*.paperead"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old in files[KEEP_EXPORTS:]:
        try:
            old.unlink()
        except OSError:
            pass


def build_router(store: LibraryStore, data_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api/sync", tags=["sync"])
    exports_dir = Path(data_dir) / EXPORT_DIR_NAME
    exports_dir.mkdir(parents=True, exist_ok=True)

    @router.post("/export")
    def export_package(body: ExportIn) -> dict[str, Any]:
        """把勾选的论文打成同步包，返回下载地址与本机路径。"""
        rows = store.list()
        by_id = {str(row["id"]): row for row in rows}
        wanted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for paper_id in body.ids[:MAX_PAPERS]:
            if paper_id in seen or paper_id not in by_id:
                continue
            seen.add(paper_id)
            wanted.append(by_id[paper_id])
        if not wanted:
            wanted = [
                row for row in rows if str(row.get("status") or "") == "ready"
            ]

        ready = [
            row
            for row in wanted
            if str(row.get("status") or "") == "ready"
            and store.blocks_path(str(row["id"])).is_file()
        ]
        skipped = [
            {
                "id": str(row["id"]),
                "title": str(row.get("title") or "未命名"),
                "reason": "还在解析" if str(row.get("status") or "") != "ready" else "缺少解析结果",
            }
            for row in wanted
            if row not in ready
        ]
        if not ready:
            raise HTTPException(status_code=400, detail="选中的论文都还没有解析完成")

        name = (
            f"paperead-sync-{time.strftime('%Y%m%d-%H%M%S')}"
            f"-{uuid.uuid4().hex[:4]}.paperead"
        )
        target = exports_dir / name
        manifest = _write_package(store, ready, target)
        _cleanup(exports_dir)
        return {
            "ok": True,
            "file": name,
            "path": str(target.resolve()),
            "url": f"/api/sync/export/{quote(name)}",
            "size": target.stat().st_size,
            "count": len(manifest["papers"]),
            "skipped": skipped[:20],
        }

    @router.get("/export/{name}")
    def download_package(name: str):
        """下载已生成的同步包；文件名只允许导出目录里的 .paperead。"""
        cleaned = safe_name(name)
        if cleaned != name or not name.endswith(".paperead"):
            raise HTTPException(status_code=404, detail="同步包不存在")
        path = (exports_dir / name).resolve()
        if path.parent != exports_dir.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="同步包不存在")
        return FileResponse(
            str(path),
            media_type="application/zip",
            filename=name,
        )

    return router