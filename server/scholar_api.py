"""文献检索的 HTTP 接口。

路由用工厂函数构造：检索本身在 `paperead.scholar` 里，写入文献库的能力由
`server.main` 注入（`store` 与「加一篇 PDF 并后台解析」的回调），避免循环依赖。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from paperead.scholar import search as scholar_search
from paperead.scholar.cite import STYLE_KEYS, build_bibtex, build_ris, format_citation
from paperead.scholar.daily import get_daily
from paperead.scholar.http import FriendlyError
from paperead.scholar.models import paper_from_dict
from paperead.scholar.search import SearchInput

log = logging.getLogger("paperead.scholar")

# 一次最多导出 100 条，避免前端误传巨大数组。
MAX_EXPORT = 100


class ImportIn(BaseModel):
    id: str = Field(..., min_length=3, max_length=300)


class CiteIn(BaseModel):
    paper: dict[str, Any]
    style: str = "gb7714"


class ExportIn(BaseModel):
    papers: list[dict[str, Any]] = Field(default_factory=list)
    format: str = "bibtex"


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def import_paper(
    store: Any,
    add_pdf: Callable[[str, bytes, dict[str, Any]], dict[str, Any]],
    public: Callable[[dict[str, Any]], dict[str, Any]],
    paper_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """按题录 id 下载 PDF 并入库；HTTP 路由与 agent 内部工具共用这一条路径。

    ``project_id`` 非空时 PDF 存在该研究项目自己名下（不进全局文献库）。
    """
    try:
        found = await scholar_search.get_paper(paper_id)
    except FriendlyError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    if not found:
        raise HTTPException(status_code=404, detail="没有找到这篇文献。")

    existing = (
        store.find_by_source(found.id, project_id=project_id)
        if project_id
        else store.find_by_source(found.id)
    )
    if existing:
        return {"paper": public(existing), "imported": False, "already": True}

    try:
        candidates = await scholar_search.resolve_pdf_urls(found)
    except FriendlyError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="这篇文献没有可下载的公开 PDF，请到原文页面获取。",
        )

    # 出版商的 PDF 链接经常挡爬虫（403），按候选顺序逐个试，任一成功即止。
    data: bytes | None = None
    last_error: FriendlyError | None = None
    for url in candidates[:4]:
        try:
            data = await scholar_search.fetch_pdf(url)
            break
        except FriendlyError as exc:
            last_error = exc
            log.info("全文下载失败，尝试下一个地址：%s（%s）", url, exc.message)
    if data is None:
        detail = last_error.message if last_error else "没有可下载的公开 PDF，请到原文页面获取。"
        raise HTTPException(
            status_code=502 if (last_error and last_error.retryable) else 400,
            detail=detail,
        )

    meta = {
        "source": found.source,
        "source_id": found.id,
        "title": found.title,
        "authors": ", ".join(found.authors[:8]),
        "abstract": found.abstract or "",
        "doi": found.doi or "",
        "url": found.url,
    }
    if project_id:
        meta["project_id"] = project_id
    row = add_pdf(scholar_search.pdf_filename(found), data, meta)
    return {"paper": public(row), "imported": True, "already": False}


def build_router(
    store: Any,
    add_pdf: Callable[[str, bytes, dict[str, Any]], dict[str, Any]],
    public: Callable[[dict[str, Any]], dict[str, Any]],
) -> APIRouter:
    """`store` 需提供 `find_by_source(source_id)`；`add_pdf` 负责落盘 + 后台解析。"""
    router = APIRouter(prefix="/api/scholar", tags=["scholar"])

    @router.get("/search")
    async def search(
        q: str = Query("", max_length=300),
        source: str = Query("arxiv"),
        sort: str = Query("rel"),
        page: int = Query(0),
        field: str = Query("all"),
        phrase: bool = Query(False),
        category: str | None = Query(None, max_length=60),
        from_year: int | None = Query(None),
        to_year: int | None = Query(None),
        per_page: int = Query(20),
    ) -> dict:
        try:
            result, used_query, translated = await scholar_search.search(
                SearchInput(
                    q=q,
                    source=source,
                    sort=sort,
                    page=page,
                    field=field,
                    phrase=phrase,
                    category=category,
                    from_year=from_year,
                    to_year=to_year,
                    per_page=per_page,
                )
            )
        except FriendlyError as exc:
            return {
                "papers": [],
                "total": 0,
                "page_limit": None,
                "used_query": q,
                "translated": False,
                "error": exc.message,
            }
        except Exception as exc:  # 兜底，避免数据源结构变化把服务打挂
            log.exception("检索失败：%s", exc)
            return {
                "papers": [],
                "total": 0,
                "page_limit": None,
                "used_query": q,
                "translated": False,
                "error": "检索服务暂时不可用，请稍后重试。",
            }
        return {
            "papers": [p.to_dict() for p in result.papers],
            "total": result.total,
            "page_limit": result.page_limit,
            "used_query": used_query,
            "translated": translated,
            "error": None,
        }

    @router.get("/sources")
    async def sources() -> dict:
        """当前配置下哪些数据源不可用（例如 CORE 未配置 API Key）。"""
        return {"disabled": scholar_search.unavailable_sources()}

    @router.get("/daily")
    async def daily(round: int = Query(0, ge=0, le=1_000_000)) -> dict:
        try:
            return await get_daily(round)
        except FriendlyError as exc:
            return {"papers": [], "date": _today(), "pool_size": 0, "error": exc.message}
        except Exception as exc:
            log.exception("每日推荐失败：%s", exc)
            return {
                "papers": [],
                "date": _today(),
                "pool_size": 0,
                "error": "推荐内容加载失败，请稍后重试。",
            }

    @router.get("/paper")
    async def paper(id: str = Query(..., min_length=3, max_length=300)) -> dict:
        try:
            found = await scholar_search.get_paper(id)
        except FriendlyError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        if not found:
            raise HTTPException(status_code=404, detail="没有找到这篇文献。")
        return found.to_dict()

    async def _fallback_pdf(doi: str) -> str | None:
        """Unpaywall 没登记时的补充来源：Semantic Scholar / CORE。"""
        for lookup in (scholar_search.find_s2_open_pdf, scholar_search.find_core_pdf):
            try:
                found = await lookup(doi)
            except Exception:  # 兜底源失败不影响 Unpaywall 的结论
                continue
            if found:
                return found
        return None

    @router.get("/fulltext")
    async def fulltext(doi: str = Query(..., min_length=3, max_length=200)) -> dict:
        try:
            result = await scholar_search.find_free_full_text(doi)
        except FriendlyError as exc:
            result = {
                "pdf_url": None,
                "landing_url": None,
                "oa_status": None,
                "error": exc.message,
            }
        except Exception as exc:
            log.exception("Unpaywall 查询失败：%s", exc)
            result = {
                "pdf_url": None,
                "landing_url": None,
                "oa_status": None,
                "error": "免费全文查询失败，请稍后重试。",
            }

        if not result.get("pdf_url"):
            extra = await _fallback_pdf(doi)
            if extra:
                result = {**result, "pdf_url": extra, "error": None}
        return result

    @router.post("/import")
    async def import_route(req: ImportIn) -> dict:
        return await import_paper(store, add_pdf, public, req.id)

    @router.post("/cite")
    async def cite(req: CiteIn) -> dict:
        style = req.style if req.style in STYLE_KEYS else "gb7714"
        paper = paper_from_dict(req.paper)
        return {"text": format_citation(paper, style)}

    @router.post("/export")
    async def export(req: ExportIn) -> dict:
        papers = [paper_from_dict(p) for p in req.papers[:MAX_EXPORT]]
        if not papers:
            raise HTTPException(status_code=400, detail="没有可导出的文献。")
        if req.format == "ris":
            return {"text": build_ris(papers), "filename": "papers.ris"}
        return {"text": build_bibtex(papers), "filename": "papers.bib"}

    return router
