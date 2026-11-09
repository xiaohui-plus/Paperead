"""给研究 agent 调用的本机内部接口（不经过 /api 的 cookie 鉴权）。

Node 侧的 `library_search` / `paper_read` 工具会带上
`x-internal-token: $PAPEREAD_INTERNAL_TOKEN`；未配置 token 时直接放行，
服务默认只监听 127.0.0.1。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from server.research.search import LibrarySearch


def _check(token: str | None) -> None:
    expected = os.environ.get("PAPEREAD_INTERNAL_TOKEN", "")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


def build_router(search: LibrarySearch, research: Any | None = None) -> APIRouter:
    router = APIRouter(prefix="/internal/research", tags=["research-internal"])

    def _project_of(session_id: str | None) -> str | None:
        """会话 → 项目：带上 sessionId 时把该项目的论文也纳入检索。"""
        if not session_id or research is None:
            return None
        session = research.get_session(session_id)
        return str(session["project_id"]) if session else None

    @router.get("/search/library")
    def search_library(
        q: str = Query(default=""),
        limit: int = Query(default=12, ge=1, le=30),
        session_id: str | None = Query(default=None, alias="sessionId"),
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict:
        _check(x_internal_token)
        return {
            "query": q.strip(),
            "results": search.search(q, limit=limit, project_id=_project_of(session_id)),
        }

    @router.get("/papers/{paper_id}/read")
    def read_paper(
        paper_id: str,
        page: int | None = Query(default=None, ge=1),
        max_chars: int = Query(default=6000, alias="maxChars", ge=500, le=30000),
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict:
        _check(x_internal_token)
        data = search.read_paper(paper_id, page=page, max_chars=max_chars)
        if data is None:
            raise HTTPException(status_code=404, detail="paper not found")
        if data.get("needsIngest"):
            raise HTTPException(
                status_code=409,
                detail={"code": "NEEDS_INGEST", "paperId": paper_id},
            )
        return data

    return router
