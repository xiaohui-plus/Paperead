"""追本溯源：引用 → 具体文献 → 原文片段。

片段来源优先级：
1. 参考文献里带 DOI → OpenAlex 按 DOI 取摘要（最准）
2. 带 arXiv 号 → arXiv API 取摘要
3. 都没有 → 按标题在 OpenAlex 找，标题匹配度足够高才用

取不到就返回 None，前端只显示文献条目 + 外部链接，不硬凑。
"""

from __future__ import annotations

import hashlib
import logging
import re

import httpx

from .references import Reference
from .search import OPENALEX, _parse

log = logging.getLogger(__name__)

_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;]+)")
_ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})", re.IGNORECASE)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")

_cache: dict[str, dict] = {}


def _link_for(ref: Reference) -> tuple[str, str]:
    """不联网也能给出的原文链接：DOI > arXiv。"""
    doi_m = _DOI_RE.search(ref.raw)
    if doi_m:
        return f"https://doi.org/{doi_m.group(1).rstrip('.')}", "doi"
    arxiv_m = _ARXIV_RE.search(ref.raw)
    if arxiv_m:
        return f"https://arxiv.org/abs/{arxiv_m.group(1)}", "arxiv"
    return "", "none"


def ref_only(ref: Reference, marker: str = "") -> dict:
    """只返回参考文献条目本身（标题/年份/链接），不抓摘要。

    接口要在一次请求里返回整段所有引用，不可能为每条都等外部接口；
    拿不到摘要的条目用这个兜底，至少保证点开有标题和可点链接。
    """
    url, source = _link_for(ref)
    return {
        "marker": marker,
        "ref": ref.to_dict(),
        "snippet": "",
        "url": url,
        "source": source,
    }


def _abstract_from_inverted(inv: dict | None) -> str:
    if not inv:
        return ""
    pos: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    pos.sort()
    return " ".join(w for _, w in pos)


async def _openalex_by_doi(doi: str) -> tuple[str, str] | None:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(f"{OPENALEX}/works/https://doi.org/{doi}")
            if r.status_code != 200:
                return None
            data = r.json()
        text = _abstract_from_inverted(data.get("abstract_inverted_index"))
        return (text, f"https://doi.org/{doi}") if text else None
    except Exception as e:
        log.info("OpenAlex DOI 查询失败 %s：%s", doi, e)
        return None


async def _arxiv_abstract(arxiv_id: str) -> tuple[str, str] | None:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(
                "http://export.arxiv.org/api/query",
                params={"id_list": arxiv_id, "max_results": 1},
            )
            r.raise_for_status()
        m = re.search(r"<summary>(.*?)</summary>", r.text, re.S)
        if not m:
            return None
        text = re.sub(r"\s+", " ", m.group(1)).strip()
        return (text, f"https://arxiv.org/abs/{arxiv_id}") if text else None
    except Exception as e:
        log.info("arXiv 查询失败 %s：%s", arxiv_id, e)
        return None


async def _openalex_by_title(title: str) -> tuple[str, str] | None:
    if len(title) < 12:
        return None
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(
                f"{OPENALEX}/works",
                params={"search": title, "per_page": "3", "mailto": "paperead@example.com"},
            )
            r.raise_for_status()
            results = r.json().get("results") or []
    except Exception as e:
        log.info("OpenAlex 标题检索失败：%s", e)
        return None

    for item in results:
        ref = _parse(item)
        # 标题相似度：去停用词后的词命中率
        a = {w.lower() for w in _WORD_RE.findall(title)}
        b = {w.lower() for w in _WORD_RE.findall(ref.title)}
        if not a or not b:
            continue
        overlap = len(a & b) / len(a)
        if overlap < 0.6:
            continue
        text = _abstract_from_inverted(item.get("abstract_inverted_index"))
        if text:
            return (text, ref.url or "")
    return None


def _best_sentence(abstract: str, context: str, limit: int = 240) -> str:
    """在摘要里挑与引用句最相关的一句。"""
    sentences = [s.strip() for s in _SENT_SPLIT.split(abstract) if len(s.strip()) > 30]
    if not sentences:
        return abstract[:limit]

    ctx = {w.lower() for w in _WORD_RE.findall(context)}

    def score(s: str) -> float:
        words = {w.lower() for w in _WORD_RE.findall(s)}
        if not words:
            return 0
        return len(words & ctx) / (len(words) ** 0.5)

    best = max(sentences, key=score)
    return best[:limit]


async def enrich(ref: Reference, context: str) -> dict:
    """给一条参考文献配上原文片段。失败返回空 snippet。"""
    key = f"{ref.index}:{ref.title[:40]}:{hashlib.sha1(context.encode('utf-8')).hexdigest()[:8]}"
    if key in _cache:
        return _cache[key]

    doi_m = _DOI_RE.search(ref.raw)
    arxiv_m = _ARXIV_RE.search(ref.raw)

    got: tuple[str, str] | None = None
    source = "none"
    if doi_m:
        got = await _openalex_by_doi(doi_m.group(1).rstrip("."))
        source = "openalex:doi"
    if not got and arxiv_m:
        got = await _arxiv_abstract(arxiv_m.group(1))
        source = "arxiv"
    if not got:
        got = await _openalex_by_title(ref.title)
        source = "openalex:title"

    result = {
        "marker": "",
        "ref": ref.to_dict(),
        "snippet": _best_sentence(got[0], context) if got else "",
        # 摘要没取到也要给出 DOI / arXiv 链接，别让「能解析」变成「什么都没有」
        "url": got[1] if got else _link_for(ref)[0],
        "source": source if got else _link_for(ref)[1],
    }
    _cache[key] = result
    return result
