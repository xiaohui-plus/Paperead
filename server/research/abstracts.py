"""为已入库论文回填题录摘要。

检索时按摘要筛选比逐篇读全文快得多，但早期入库的论文、以及手动上传的 PDF
只有正文。这里按 source_id → DOI → 标题的顺序，去原始数据源 / OpenAlex
查一次摘要并写回 library.json；拿不到就保持为空，不做猜测。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote, urlencode

from paperead.scholar.http import CONTACT_EMAIL, FriendlyError, fetch_text
from paperead.scholar.search import get_paper
from paperead.scholar.sources import (
    OPENALEX_API,
    OPENALEX_SELECT,
    SEMANTIC_SCHOLAR_API,
    parse_openalex_works,
    sanitize_filter_value,
)

log = logging.getLogger("paperead.research.abstracts")

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)
TITLE_MATCH = 0.85


def find_doi(text: str) -> str:
    """从首页正文里找 DOI；PDF 断行会把 DOI 拆开，先去掉空白再匹配。"""
    compact = re.sub(r"\s+", "", text or "")
    found = DOI_RE.search(compact)
    # 句尾标点也会被 DOI 字符集吃进来，去掉后再查。
    return found.group(0)[:120].rstrip(".,;:") if found else ""


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _similar(a: str, b: str) -> float:
    left, right = _normalize(a), _normalize(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


async def _from_source(source_id: str) -> str:
    if ":" not in source_id:
        return ""
    try:
        paper = await get_paper(source_id)
    except FriendlyError:
        return ""
    except Exception as exc:  # 单个数据源失败不影响后续 DOI/标题兜底
        log.info("详情查询失败 %s：%s", source_id, exc)
        return ""
    return (paper.abstract or "").strip() if paper else ""


async def _openalex_work(query: str) -> list[Any]:
    """按 OpenAlex 过滤条件取最多 5 条，供 DOI / 标题两种查询复用。"""
    params = {
        "filter": query,
        "per-page": "5",
        "select": OPENALEX_SELECT,
        "mailto": CONTACT_EMAIL,
    }
    payload = json.loads(await fetch_text(f"{OPENALEX_API}?{urlencode(params)}"))
    return parse_openalex_works(payload)


async def _from_doi(doi: str) -> str:
    try:
        works = await _openalex_work(f"doi:{quote(doi, safe='/')}")
    except Exception as exc:
        log.info("OpenAlex DOI 查询失败 %s：%s", doi, exc)
        works = []
    for work in works:
        if (work.abstract or "").strip():
            return work.abstract.strip()

    # OpenAlex 偶尔没有摘要；Semantic Scholar 一般有。
    try:
        url = (
            f"{SEMANTIC_SCHOLAR_API}/paper/DOI:{quote(doi, safe='/')}"
            "?fields=title,abstract"
        )
        payload = json.loads(await fetch_text(url))
    except Exception as exc:
        log.info("Semantic Scholar DOI 查询失败 %s：%s", doi, exc)
        return ""
    return (payload.get("abstract") or "").strip()


async def _from_title(title: str) -> str:
    cleaned = sanitize_filter_value(title)
    if len(cleaned) < 8:
        return ""
    try:
        works = await _openalex_work(f"title.search:{cleaned}")
    except Exception as exc:
        log.info("OpenAlex 标题查询失败 %s：%s", title, exc)
        return ""
    best = max(works, key=lambda item: _similar(item.title, title), default=None)
    if best is None or _similar(best.title, title) < TITLE_MATCH:
        return ""
    return (best.abstract or "").strip()


async def lookup_abstract(title: str, source_id: str = "", doi: str = "") -> str:
    """按可信度从高到低查询摘要，全部失败时返回空串。"""
    if source_id:
        abstract = await _from_source(source_id)
        if abstract:
            return abstract
    if doi:
        abstract = await _from_doi(doi)
        if abstract:
            return abstract
    if title:
        return await _from_title(title)
    return ""


async def fill_one(store: Any, paper_id: str) -> bool:
    """给单篇论文补摘要；已经有摘要的直接跳过。"""
    row = store.get(paper_id)
    if not row or (row.get("abstract") or "").strip():
        return False
    blocks = store.read_blocks(paper_id) or []
    lead = " ".join(str(block.get("text") or "") for block in blocks[:30])
    doi = str(row.get("doi") or "").strip() or find_doi(lead)
    abstract = await lookup_abstract(
        str(row.get("title") or ""), str(row.get("source_id") or ""), doi
    )
    if not abstract:
        return False
    store.update(paper_id, abstract=abstract, abstract_source="lookup")
    return True


async def backfill(
    store: Any, *, only_missing: bool = True, concurrency: int = 4
) -> dict[str, Any]:
    """批量回填；返回总数、成功数与仍未拿到摘要的 paperId。"""
    rows = store.list(include_project=True)
    if only_missing:
        rows = [row for row in rows if not (row.get("abstract") or "").strip()]

    semaphore = asyncio.Semaphore(concurrency)
    filled = 0
    missing: list[str] = []

    async def run(row: dict[str, Any]) -> None:
        nonlocal filled
        async with semaphore:
            try:
                ok = await fill_one(store, row["id"])
            except Exception as exc:  # 单篇异常只记录，不中断整批
                log.info("回填失败 %s：%s", row["id"], exc)
                ok = False
        if ok:
            filled += 1
        else:
            missing.append(row["id"])

    await asyncio.gather(*(run(row) for row in rows))
    return {"total": len(rows), "filled": filled, "missing": missing}
