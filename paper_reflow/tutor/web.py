"""网络搜索：默认 Tavily，没配 key 就返回空（不阻塞论文内问答）。"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"


def has_key() -> bool:
    return bool(os.getenv("TAVILY_API_KEY"))


async def search(query: str, limit: int = 4) -> list[dict]:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        log.info("未配置 TAVILY_API_KEY，跳过网络搜索")
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                TAVILY_URL,
                json={
                    "api_key": key,
                    "query": query,
                    "max_results": limit,
                    "search_depth": "basic",
                    "include_answer": False,
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("Tavily 搜索失败：%s", e)
        return []

    out = []
    for item in data.get("results") or []:
        out.append(
            {
                "title": (item.get("title") or "").strip()[:160],
                "url": item.get("url") or "",
                "content": (item.get("content") or "").strip()[:600],
            }
        )
    return out[:limit]
