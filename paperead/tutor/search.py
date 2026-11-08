"""检索适配层：默认直连 OpenAlex（免费、限流宽松）。

主项目里有更完整的检索栈（PubMed / arXiv / S2 / PMC），这里做成适配器，
配置 `PAPEREAD_SEARCH_BASE` 后可以改走主项目。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

from ..env import env

log = logging.getLogger(__name__)

OPENALEX = env("PAPEREAD_OPENALEX", "https://api.openalex.org")
MAILTO = env("PAPEREAD_MAILTO", "paperead@example.com")

_cache: dict[str, tuple[float, list["Ref"]]] = {}
_TTL = 6 * 3600


@dataclass
class Ref:
    id: str
    title: str
    year: int | None = None
    cites: int = 0
    doi: str | None = None
    url: str | None = None
    is_review: bool = False
    authors: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "year": self.year,
            "cites": self.cites,
            "doi": self.doi,
            "url": self.url,
            "is_review": self.is_review,
            "authors": self.authors,
        }


def _parse(item: dict) -> Ref:
    doi = (item.get("doi") or "").replace("https://doi.org/", "") or None
    authorships = item.get("authorships") or []
    first = ""
    if authorships:
        first = (authorships[0].get("author") or {}).get("display_name") or ""
    return Ref(
        id=str(item.get("id") or "").rsplit("/", 1)[-1],
        title=(item.get("title") or item.get("display_name") or "").strip(),
        year=item.get("publication_year"),
        cites=int(item.get("cited_by_count") or 0),
        doi=doi,
        url=(item.get("primary_location") or {}).get("landing_page_url") or item.get("doi"),
        is_review=(item.get("type") or "") == "review",
        authors=first,
    )


async def search(query: str, limit: int = 5, min_year: int | None = None) -> list[Ref]:
    """按标题/关键词检索文献，按被引次数降序。"""
    key = f"{query}|{limit}|{min_year}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    params = {
        "search": query,
        "per_page": str(limit),
        "sort": "cited_by_count:desc",
        "mailto": MAILTO,
    }
    if min_year:
        params["filter"] = f"from_publication_date:{min_year}-01-01"

    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(f"{OPENALEX}/works", params=params)
            r.raise_for_status()
            refs = [_parse(x) for x in (r.json().get("results") or []) if x.get("title")]
    except Exception as e:
        log.warning("OpenAlex 检索失败（%s）：%s", query, e)
        return []

    _cache[key] = (time.time(), refs)
    return refs


async def lookup_title(title: str, year: int | None = None) -> Ref | None:
    refs = await search(title, limit=3)
    if not refs:
        return None
    if year:
        same = [r for r in refs if r.year and abs(r.year - year) <= 1]
        if same:
            return same[0]
    return refs[0]
