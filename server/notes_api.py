"""外部笔记接口：/api/notes。

只做本地导入与检索；内容不进安装包，来源与许可随每条记录一起返回。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from paperead.notes_import import collect_notes, parse_note
from server.notes_catalog import NotesCatalog

from paperead.notes_import import _parse_front_matter
from server.notes_http import (
    MAX_PDF_BYTES,
    http_download,
    http_get_bytes,
    http_get_prefix_any,
    http_get_prefix_many,
    http_get_text_any,
)
from server.notes_mirror import NotesMirror
from server.notes_search import NoteSearch
from server.notes_store import NoteStore

_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_PDF_HOSTS = ("openaccess.thecvf.com", "arxiv.org", "proceedings.neurips.cc", "aclanthology.org")


class ImportIn(BaseModel):
    path: str = Field(..., min_length=1)
    limit: int = 0


class MirrorInstallIn(BaseModel):
    conference: str = Field(..., min_length=4)
    url: str = Field(..., min_length=8)


class MirrorLocalIn(BaseModel):
    conference: str = Field(..., min_length=4)
    path: str = Field(..., min_length=1)


def _to_pdf_url(url: str) -> str:
    """把论文摘要页换成 PDF 直链：arXiv abs、CVF html 都有稳定的对应规则。"""
    if "arxiv.org/abs/" in url:
        raw = url.split("arxiv.org/abs/", 1)[1]
        raw = raw.split("?", 1)[0].split("#", 1)[0].strip("/")
        return f"https://arxiv.org/pdf/{raw}"
    if "openaccess.thecvf.com" in url and "/html/" in url:
        base = url.split("?", 1)[0].split("#", 1)[0]
        if base.endswith(".html"):
            return base.replace("/html/", "/papers/")[: -len(".html")] + ".pdf"
    return url


def _candidate_urls(blocks: list[dict[str, Any]]) -> list[str]:
    """从笔记正文的 markdown 链接里挑最像论文 PDF 的地址。"""
    found: list[tuple[int, str]] = []
    for block in blocks:
        for label, url in _LINK_RE.findall(str(block.get("text") or "")):
            low = url.lower()
            score = 0
            if low.endswith(".pdf"):
                score += 10
            if "arxiv.org/abs/" in low:
                score += 6
            if "arxiv.org/pdf/" in low:
                score += 8
            if any(host in low for host in _PDF_HOSTS):
                score += 4
            if "论文" in label or "pdf" in label.lower():
                score += 3
            if score:
                found.append((score, _to_pdf_url(url)))
    found.sort(key=lambda item: item[0], reverse=True)

    seen: set[str] = set()
    urls: list[str] = []
    for _, url in found:
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


async def _download_pdf(url: str) -> bytes:
    try:
        data = await asyncio.to_thread(http_get_bytes, url, 60, MAX_PDF_BYTES)
    except ValueError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"下载失败：{error}") from error
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="链接返回的不是 PDF")
    return data


ONLINE_SOURCES = (
    "https://cdn.jsdelivr.net/gh/M0rtzz/paper-notes@main/{path}",
    "https://raw.githubusercontent.com/M0rtzz/paper-notes/main/{path}",
)

# 摘要只取文件头（Range 请求）：jsDelivr 对批量 Range 返回空，raw 更快且稳定。
DESCRIPTION_SOURCES = (ONLINE_SOURCES[1], ONLINE_SOURCES[0])


def _fetch_markdown(path: str) -> str:
    """按源逐个尝试；jsDelivr 对国内更友好，raw 作后备。"""
    encoded = quote(path, safe="/")
    urls = [template.format(path=encoded) for template in ONLINE_SOURCES]
    try:
        text = http_get_text_any(urls, timeout=8)
    except Exception as error:
        raise HTTPException(
            status_code=502, detail=f"笔记加载失败：{str(error)[:80]}"
        ) from error
    if not text.lstrip().startswith(("---", "#")):
        raise HTTPException(status_code=502, detail="返回内容不是笔记")
    return text


def _parse_online(path: str, mirror: NotesMirror | None = None) -> Any:
    """把 md 落到临时目录再解析，保证 source_path 与本地导入一致。

    有本地镜像时直接读镜像文件，不再走网络。
    """
    rel = path[len("docs/") :]
    if mirror is not None:
        local = mirror.note_path(rel)
        if local is not None:
            return parse_note(local, root=mirror.root / "docs")
    root = Path(tempfile.mkdtemp(prefix="paperead-note-"))
    docs = root / "docs"
    target = docs / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_fetch_markdown(path), encoding="utf-8")
        return parse_note(target, root=docs)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _description_from_text(text: str) -> str:
    if not text:
        return ""
    meta, _ = _parse_front_matter(text)
    return str(meta.get("description") or "").strip()


def _mirror_description(path: str, mirror: NotesMirror | None) -> str | None:
    """镜像里有这篇就直接读本地；返回 None 表示需要联网。"""
    if mirror is None:
        return None
    rel = path[len("docs/") :]
    local = mirror.note_path(rel)
    if local is None:
        return None
    try:
        text = local.read_text(encoding="utf-8")[:8192]
    except OSError:
        return None
    if not text:
        return None
    return _description_from_text(text)


def _fetch_description(path: str, mirror: NotesMirror | None = None) -> tuple[str, str]:
    local = _mirror_description(path, mirror)
    if local is not None:
        return path, local

    encoded = quote(path, safe="/")
    urls = [template.format(path=encoded) for template in ONLINE_SOURCES]
    try:
        text = http_get_prefix_any(urls, 8192, timeout=6)
    except Exception:
        return path, ""
    return path, _description_from_text(text)


def _fetch_descriptions(
    paths: list[str], mirror: NotesMirror | None = None
) -> dict[str, str]:
    """镜像优先；未命中的摘要整批用一次 curl 取，避免每篇各起一个进程。"""
    if not paths:
        return {}

    out: dict[str, str] = {}
    pending: list[str] = []
    for path in paths:
        local = _mirror_description(path, mirror)
        if local is not None:
            out[path] = local
        else:
            pending.append(path)

    for template in DESCRIPTION_SOURCES:
        if not pending:
            break
        urls = [template.format(path=quote(path, safe="/")) for path in pending]
        try:
            texts = http_get_prefix_many(urls, 8192, timeout=12)
        except Exception:
            continue
        retry: list[str] = []
        for path, text in zip(pending, texts):
            desc = _description_from_text(text)
            if desc:
                out[path] = desc
            else:
                retry.append(path)
        pending = retry

    # 只剩零星几条时再用单篇回退；整批失败不逐条重试，避免又拉起几十个进程。
    if 0 < len(pending) <= 5:
        for path, desc in (
            _fetch_description(path, mirror=None) for path in pending
        ):
            out[path] = desc
    for path in pending:
        out.setdefault(path, "")
    return out


def build_router(
    store: NoteStore,
    search: NoteSearch,
    paper_store: Any,
    parse_task: Callable[[str], Awaitable[None]],
    public: Callable[[dict[str, Any]], dict[str, Any]],
    catalog: NotesCatalog | None = None,
    mirror: NotesMirror | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/notes", tags=["notes"])

    @router.post("/import")
    def import_notes(payload: ImportIn):
        target = Path(payload.path).expanduser()
        if not target.exists():
            raise HTTPException(status_code=404, detail="路径不存在")
        if target.is_dir():
            notes = collect_notes(target, limit=max(0, int(payload.limit)))
        else:
            notes = [parse_note(target)]
        if not notes:
            raise HTTPException(status_code=400, detail="该路径下没有可导入的笔记")
        saved = [store.upsert(note) for note in notes]
        search.refresh(force=True)
        return {"imported": len(saved), "total": store.count(), "notes": saved[:50]}

    @router.get("")
    def list_notes():
        return {"notes": store.list(), "total": store.count()}

    # 必须排在 /{note_id} 前面，否则 search 会被当成 note_id。
    @router.get("/search")
    def search_notes(q: str = Query(""), limit: int = 12):
        return {"query": q, "results": search.search(q, limit=limit)}

    @router.get("/catalog")
    def note_catalog(
        conference: str = "",
        field: str = "",
        q: str = "",
        limit: int = 60,
        offset: int = 0,
    ):
        if catalog is None:
            raise HTTPException(status_code=503, detail="笔记目录未启用")
        status = catalog.ensure()
        data = catalog.list(
            conference=conference,
            field=field,
            query=q,
            limit=limit,
            offset=offset,
        )
        # 已缓存到本地的笔记有真实标题，覆盖 slug 推断的占位标题。
        titles = {
            row["source_path"]: row.get("title") or ""
            for row in store.list()
            if row.get("source_path")
        }
        for item in data["items"]:
            title = titles.get(item["path"][len("docs/") :])
            if title:
                item["title"] = title
            item["description"] = catalog.description_of(item["path"])
        return {
            **status,
            **data,
            "conferences": catalog.conferences(),
            "fields": catalog.fields(conference),
        }

    @router.get("/catalog/overview")
    def catalog_overview():
        if catalog is None:
            raise HTTPException(status_code=503, detail="笔记目录未启用")
        catalog.ensure()
        return {"status": catalog.status(), **catalog.overview()}

    @router.get("/catalog/field")
    def catalog_field(
        conference: str = Query(""),
        field: str = Query(""),
        limit: int = 30,
        offset: int = 0,
    ):
        if catalog is None:
            raise HTTPException(status_code=503, detail="笔记目录未启用")
        catalog.ensure()
        data = catalog.list(
            conference=conference, field=field, limit=limit, offset=offset
        )
        # 列表立即返回；摘要由前端分批调用 /catalog/descriptions 补齐，
        # 否则 30 篇 × 两个源的网络等待会把接口拖到一分钟以上。
        for item in data["items"]:
            item["description"] = catalog.description_of(item["path"])
        return {
            **data,
            "across": catalog.field_across(field),
            "themes": catalog.themes(conference, field),
        }

    @router.get("/catalog/descriptions")
    def catalog_descriptions(paths: str = Query("")):
        if catalog is None:
            raise HTTPException(status_code=503, detail="笔记目录未启用")
        wanted = [part.strip() for part in paths.split(",") if part.strip()][:30]
        missing = [path for path in wanted if not catalog.description_of(path)]
        if missing:
            catalog.remember_descriptions(_fetch_descriptions(missing, mirror))
        return {"descriptions": {path: catalog.description_of(path) for path in wanted}}

    @router.get("/mirror")
    def mirror_status():
        if mirror is None:
            raise HTTPException(status_code=503, detail="镜像未启用")
        return mirror.sync_status()

    @router.post("/mirror/install")
    def mirror_install(payload: MirrorInstallIn):
        if mirror is None:
            raise HTTPException(status_code=503, detail="镜像未启用")
        if not payload.url.startswith(("https://", "http://")):
            raise HTTPException(status_code=400, detail="镜像地址必须是 http(s)")
        tmp = mirror.root / f".{payload.conference}.tar.gz"
        try:
            http_download(payload.url, tmp, timeout=900)
            return mirror.extract(tmp, payload.conference)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        finally:
            tmp.unlink(missing_ok=True)

    @router.post("/mirror/install-local")
    def mirror_install_local(payload: MirrorLocalIn):
        if mirror is None:
            raise HTTPException(status_code=503, detail="镜像未启用")
        archive = Path(payload.path).expanduser()
        if not archive.is_file():
            raise HTTPException(status_code=404, detail="镜像包不存在")
        try:
            return mirror.extract(archive, payload.conference)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/catalog/refresh")
    def refresh_catalog():
        if catalog is None:
            raise HTTPException(status_code=503, detail="笔记目录未启用")
        catalog.refresh_async()
        return catalog.status()

    @router.get("/online")
    def online_note(path: str = Query(...)):
        if not path.startswith("docs/") or not path.endswith(".md") or ".." in path:
            raise HTTPException(status_code=400, detail="笔记路径不合法")
        rel = path[len("docs/") :]
        cached = store.get(store.note_id(rel))
        if cached and cached.get("blocks"):
            return {"note": cached, "cached": True}
        note = _parse_online(path, mirror)
        if not note.blocks:
            raise HTTPException(status_code=502, detail="笔记内容为空")
        saved = store.upsert(note)
        payload = store.get(saved["id"]) or note.to_dict()
        return {"note": payload, "cached": False}

    @router.get("/{note_id}")
    def get_note(note_id: str):
        note = store.get(note_id)
        if not note:
            raise HTTPException(status_code=404, detail="笔记不存在")
        return note

    @router.post("/{note_id}/import-paper")
    async def import_note_paper(note_id: str):
        """从笔记里的论文链接下载 PDF 并入库，随后把笔记与论文关联。"""
        note = store.get(note_id)
        if not note:
            raise HTTPException(status_code=404, detail="笔记不存在")

        linked = str(note.get("paper_id") or "")
        if linked and paper_store.get(linked):
            return {
                "paper": public(paper_store.get(linked)),
                "created": False,
                "cached": True,
            }

        urls = _candidate_urls(note.get("blocks") or [])
        if not urls:
            raise HTTPException(
                status_code=400, detail="笔记里没有可下载的论文 PDF 链接"
            )

        errors: list[str] = []
        for url in urls[:3]:
            try:
                data = await _download_pdf(url)
            except HTTPException as error:
                errors.append(f"{error.detail}")
                continue

            existing = paper_store.find_by_pdf_hash(data)
            if existing is not None:
                row = existing
                created = False
            else:
                filename = Path(urlparse(url).path).name or "paper.pdf"
                row = paper_store.add(filename, data)
                created = True
                asyncio.create_task(parse_task(row["id"]))

            store.link_paper(note_id, row["id"])
            return {
                "paper": public(row),
                "url": url,
                "created": created,
                "cached": False,
            }

        raise HTTPException(
            status_code=502,
            detail="下载论文 PDF 失败：" + ("；".join(errors[:3]) or "没有可用链接"),
        )

    return router