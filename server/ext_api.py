"""浏览器插件接口：/api/ext/v1。

只做采集入库，不做阅读。主应用鉴权仍由 server.main.guard 负责；
本模块校验插件 token 与扩展 Origin。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from paperead import __version__
from server.plugin_auth import PairingCodes, PairingError, Plugin, PluginRegistry, is_extension_origin

MAX_PDF_BYTES = 100 * 1024 * 1024
META_FIELDS = (
    "source",
    "source_id",
    "title",
    "authors",
    "abstract",
    "doi",
    "url",
    "venue",
)


class PairIn(BaseModel):
    code: str = Field("", max_length=12)
    name: str = ""


class PairSessionIn(BaseModel):
    name: str = ""


def _open_url(request: Request, paper_id: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/?p={paper_id}"


def _normalize_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip()
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _clean_meta(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("metadata 必须是对象")
    meta: dict[str, Any] = {}
    for key in META_FIELDS:
        value = str(raw.get(key) or "").strip()
        if value:
            meta[key] = value
    year = raw.get("year")
    if year not in (None, ""):
        try:
            meta["year"] = int(year)
        except (TypeError, ValueError):
            pass
    return meta


def _safe_filename(name: str, paper_id: str) -> str:
    stem = Path(name or "paper").stem
    stem = re.sub(r"[^\w.\- ]+", "_", stem).strip(" .")[:80]
    return f"{stem or paper_id}.pdf"


def build_router(
    *,
    store: Any,
    registry: PluginRegistry,
    parse_task: Callable[[str], Awaitable[None]],
    public: Callable[[dict[str, Any]], dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/api/ext/v1", tags=["ext"])
    codes = PairingCodes()

    def require_plugin(
        request: Request, authorization: str | None = Header(default=None)
    ) -> Plugin:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少插件 token")
        token = authorization.removeprefix("Bearer ").strip()
        plugin = registry.verify(token, request.headers.get("origin"))
        if plugin is None:
            raise HTTPException(status_code=401, detail="插件 token 无效或已撤销")
        return plugin

    @router.get("/ping")
    async def ping() -> dict:
        return {"ok": True, "app": "paperead", "api": 1, "version": __version__}

    @router.post("/pair/session")
    async def pair_session(payload: PairSessionIn) -> dict:
        code, expires_at = codes.issue()
        return {"code": code, "expires_at": expires_at}

    @router.post("/pair/wait")
    async def pair_wait() -> dict:
        """桌面端点击「连接浏览器插件」后开一个等待窗口。

        不返回任何码：扩展在该窗口内不带码调用 /pair 即可绑定。
        """
        return {"expires_at": codes.open_wait()}

    @router.post("/pair")
    async def pair(request: Request, payload: PairIn) -> dict:
        origin = request.headers.get("origin", "")
        if not is_extension_origin(origin):
            raise HTTPException(status_code=403, detail="只允许浏览器扩展配对")
        client = request.client.host if request.client else "unknown"
        try:
            codes.consume(client, payload.code)
        except PairingError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        token, plugin = registry.issue(origin, payload.name)
        return {"token": token, "api": 1, "scopes": plugin.scopes}

    @router.get("/papers/lookup")
    async def lookup(
        request: Request,
        source_id: str = Query(""),
        doi: str = Query(""),
        url: str = Query(""),
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_plugin(request, authorization)
        row = store.find_by_source(source_id) if source_id else None
        if row is None and doi:
            needle = doi.strip().lower()
            row = next(
                (item for item in store.list() if str(item.get("doi") or "").lower() == needle),
                None,
            )
        if row is None and url:
            needle = _normalize_url(url)
            row = next(
                (
                    item
                    for item in store.list()
                    if item.get("url") and _normalize_url(str(item["url"])) == needle
                ),
                None,
            )
        if row is None:
            return {"found": False, "paper": None}
        return {
            "found": True,
            "paper": {**public(row), "open_url": _open_url(request, row["id"])},
        }

    @router.post("/papers/ingest")
    async def ingest(
        request: Request,
        file: UploadFile = File(...),
        metadata: str = Form("{}"),
        folder_id: str = Form(""),
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_plugin(request, authorization)
        data = await file.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="PDF 超过 100MB")
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data:
            raise HTTPException(status_code=400, detail="不是有效的 PDF")
        if folder_id and store.get_folder(folder_id) is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        try:
            meta = _clean_meta(json.loads(metadata or "{}"))
        except (ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if folder_id:
            meta["folder_id"] = folder_id

        existing = store.find_by_source(meta.get("source_id", ""))
        if existing is None:
            existing = store.find_by_pdf_hash(data, folder_id=folder_id or None)
        if existing is not None:
            if existing.get("status") != "ready":
                asyncio.create_task(parse_task(existing["id"]))
            return {
                "paper": {**public(existing), "open_url": _open_url(request, existing["id"])},
                "already": True,
            }

        filename = _safe_filename(file.filename or "paper.pdf", "paper")
        row = store.add(filename, data, meta or None)
        asyncio.create_task(parse_task(row["id"]))
        return {
            "paper": {**public(row), "open_url": _open_url(request, row["id"])},
            "already": False,
        }

    @router.get("/papers/{paper_id}")
    async def paper_status(
        request: Request,
        paper_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_plugin(request, authorization)
        row = store.get(paper_id)
        if row is None or row.get("project_id"):
            raise HTTPException(status_code=404, detail="论文不存在")
        return {
            "paper": public(row),
            "open_url": _open_url(request, paper_id),
        }

    @router.get("/folders")
    async def folders(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_plugin(request, authorization)
        return {
            "folders": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "count": item.get("paper_count", 0),
                }
                for item in store.list_folders()
            ]
        }

    @router.get("/admin/plugins")
    async def admin_plugins() -> dict:
        return {"plugins": registry.list_public()}

    @router.delete("/admin/plugins/{plugin_id}")
    async def admin_revoke(plugin_id: str) -> dict:
        if not registry.revoke(plugin_id):
            raise HTTPException(status_code=404, detail="插件不存在")
        return {"ok": True}

    return router
