"""统一检索入口：四个源的分派、单篇详情、开放获取全文、中文查询译英。

上层（FastAPI）只调用这里，不直接碰数据源。所有错误都是 `FriendlyError`，
`message` 可以直接展示给用户。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from urllib.parse import quote

from .http import (
    CONTACT_EMAIL,
    TTL_DETAIL,
    TTL_PROBE,
    TTL_TRANSLATE,
    TTL_UNPAYWALL,
    FriendlyError,
    arxiv_fetch,
    cache,
    fetch_bytes,
    fetch_once,
    fetch_text,
    looks_like_pdf,
    safe_public_url,
    with_arxiv_gate,
)
from .models import Paper
from .sources import (
    ARXIV_API,
    EPMC_API,
    OPENALEX_API,
    OPENALEX_SELECT,
    PUBMED_EUTILS,
    arxiv_url,
    epmc_detail_query,
    europe_pmc_url,
    openalex_url,
    parse_arxiv_entries,
    parse_europe_pmc,
    parse_openalex_works,
    parse_pubmed_abstract_xml,
    parse_pubmed_summary,
    pubmed_urls,
    unpaywall_url,
)

log = logging.getLogger(__name__)

SOURCE_KEYS = ("arxiv", "pubmed", "openalex", "europepmc")
SORT_KEYS = ("rel", "new", "updated")
FIELD_KEYS = ("all", "title", "abstract", "author")


@dataclass
class SearchInput:
    q: str
    source: str = "arxiv"
    sort: str = "rel"
    page: int = 0
    field: str = "all"
    phrase: bool = False
    category: str | None = None
    from_year: int | None = None
    to_year: int | None = None
    per_page: int = 20

    def clean(self) -> "SearchInput":
        """把外部传入的参数夹到安全范围，非法值退回默认。"""
        q = (self.q or "").strip()[:300]
        page = max(0, min(int(self.page or 0), 50))
        per_page = max(10, min(int(self.per_page or 20), 50))
        source = self.source if self.source in SOURCE_KEYS else "arxiv"
        sort = self.sort if self.sort in SORT_KEYS else "rel"
        field = self.field if self.field in FIELD_KEYS else "all"
        return replace(
            self,
            q=q,
            source=source,
            sort=sort,
            field=field,
            page=page,
            per_page=per_page,
            category=(self.category or "").strip()[:60] or None,
            from_year=self.from_year if _year_ok(self.from_year) else None,
            to_year=self.to_year if _year_ok(self.to_year) else None,
        )


def _year_ok(value: int | None) -> bool:
    return isinstance(value, int) and 1000 <= value <= 2200


@dataclass
class SearchResult:
    papers: list[Paper]
    total: int = 0
    page_limit: int | None = None


# ── 各源检索 ────────────────────────────────────────────────


async def _search_arxiv(data: SearchInput) -> SearchResult:
    url = arxiv_url(
        data.q,
        field=data.field,
        phrase=data.phrase,
        category=data.category,
        from_year=data.from_year,
        to_year=data.to_year,
        sort=data.sort,
        page=data.page,
        per_page=data.per_page,
    )
    xml = await arxiv_fetch(url)
    match = re.search(r"<opensearch:totalResults[^>]*>(\d+)", xml)
    total = int(match.group(1)) if match else 0
    return SearchResult(parse_arxiv_entries(xml), total, None)


async def _search_pubmed(data: SearchInput) -> SearchResult:
    search_url = pubmed_urls(
        data.q,
        field=data.field,
        phrase=data.phrase,
        from_year=data.from_year,
        to_year=data.to_year,
        sort=data.sort,
        page=data.page,
        per_page=data.per_page,
    )
    payload = json.loads(await fetch_text(search_url))
    result = payload.get("esearchresult") or {}
    ids = [str(x) for x in (result.get("idlist") or [])]
    total = int(result.get("count") or 0)
    if not ids:
        return SearchResult([], total, None)
    summaries = await _pubmed_summaries(ids)
    return SearchResult([parse_pubmed_summary(s) for s in summaries], total, None)


async def _search_openalex(data: SearchInput) -> SearchResult:
    url = openalex_url(
        data.q,
        field=data.field,
        phrase=data.phrase,
        from_year=data.from_year,
        to_year=data.to_year,
        sort=data.sort,
        page=data.page,
        per_page=data.per_page,
        mailto=CONTACT_EMAIL,
    )
    payload = json.loads(await fetch_text(url))
    total = int((payload.get("meta") or {}).get("count") or 0)
    return SearchResult(parse_openalex_works(payload), total, None)


async def _search_europe_pmc(data: SearchInput) -> SearchResult:
    url, page_limit = europe_pmc_url(
        data.q,
        field=data.field,
        phrase=data.phrase,
        from_year=data.from_year,
        to_year=data.to_year,
        sort=data.sort,
        page=data.page,
        per_page=data.per_page,
    )
    page = min(data.page, page_limit - 1)
    offset = page * data.per_page
    payload = json.loads(await fetch_text(url))
    papers = parse_europe_pmc(payload)[offset : offset + data.per_page]
    return SearchResult(papers, int(payload.get("hitCount") or 0), page_limit)


async def search(data: SearchInput) -> tuple[SearchResult, str, bool]:
    """返回 (结果, 实际检索词, 是否做过中译英)。异常为 FriendlyError。"""
    data = data.clean()
    if not data.q:
        raise FriendlyError("请输入检索词。", False)

    used_query, translated = await to_english_query(data.q)
    if translated:
        data = replace(data, q=used_query)

    runner = {
        "arxiv": _search_arxiv,
        "pubmed": _search_pubmed,
        "openalex": _search_openalex,
        "europepmc": _search_europe_pmc,
    }[data.source]
    return await runner(data), used_query, translated


# ── 详情 ────────────────────────────────────────────────────


async def get_paper(paper_id: str) -> Paper | None:
    if ":" not in paper_id:
        return None
    source, raw_id = paper_id.split(":", 1)
    try:
        if source == "arxiv":
            return await _detail_arxiv(raw_id)
        if source == "openalex":
            return await _detail_openalex(raw_id)
        if source == "europepmc":
            return await _detail_europepmc(raw_id)
        if source == "pubmed":
            return await _detail_pubmed(raw_id)
    except FriendlyError:
        raise
    except Exception as exc:  # 解析异常也当作「没有这条记录」，避免 500
        log.warning("详情解析失败 %s：%s", paper_id, exc)
        return None
    return None


async def _detail_arxiv(raw_id: str) -> Paper | None:
    url = f"{ARXIV_API}?id_list={quote(raw_id, safe='')}"
    # 元数据与 HTML 探测都要过同一个节流闸门，实际是串行；两者都带缓存，重复访问直接命中。
    xml = await arxiv_fetch(url, ttl=TTL_DETAIL)
    html_ok = await has_arxiv_html(raw_id)
    papers = parse_arxiv_entries(xml)
    if not papers:
        return None
    paper = papers[0]
    if not html_ok:
        paper.html_url = None
    return paper


async def has_arxiv_html(raw_id: str) -> bool:
    """探测 arXiv HTML 全文页是否可用；不确定时默认可用，由前端兜底。"""
    url = f"https://arxiv.org/html/{raw_id}"

    async def probe() -> str:
        async def hit() -> str:
            response = await fetch_once(url, 5)
            return "1" if response.status_code < 400 else "0"

        return await with_arxiv_gate(hit)

    try:
        return (await cache.get(url, TTL_PROBE, probe)) == "1"
    except Exception:
        return True


async def _detail_openalex(raw_id: str) -> Paper | None:
    url = (
        f"{OPENALEX_API}/{quote(raw_id, safe='')}"
        f"?select={quote(OPENALEX_SELECT, safe=',')}&mailto={quote(CONTACT_EMAIL, safe='')}"
    )
    payload = json.loads(await fetch_text(url, ttl=TTL_DETAIL))
    works = parse_openalex_works({"results": [payload] if payload.get("id") else []})
    return works[0] if works else None


async def _detail_europepmc(raw_id: str) -> Paper | None:
    query = epmc_detail_query(f"europepmc:{raw_id}")
    if not query:
        return None
    url = (
        f"{EPMC_API}?query={quote(query, safe='')}"
        "&format=json&resultType=core&pageSize=1"
    )
    payload = json.loads(await fetch_text(url, ttl=TTL_DETAIL))
    papers = parse_europe_pmc(payload)
    return papers[0] if papers else None


async def _detail_pubmed(raw_id: str) -> Paper | None:
    summaries = await _pubmed_summaries([raw_id])
    if not summaries:
        return None
    xml = await fetch_text(
        f"{PUBMED_EUTILS}/efetch.fcgi?db=pubmed&retmode=xml&id={quote(raw_id, safe='')}",
        ttl=TTL_DETAIL,
    )
    abstract, keywords = parse_pubmed_abstract_xml(xml)
    return parse_pubmed_summary(summaries[0], abstract, keywords)


async def _pubmed_summaries(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    url = (
        f"{PUBMED_EUTILS}/esummary.fcgi?db=pubmed&retmode=json"
        f"&id={quote(','.join(ids), safe=',')}"
    )
    payload = json.loads(await fetch_text(url))
    result = payload.get("result") or {}
    return [result[i] for i in ids if isinstance(result.get(i), dict)]


# ── 开放获取全文 ────────────────────────────────────────────


async def find_free_full_text(doi: str) -> dict:
    bare = re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip(), flags=re.I)
    if not bare:
        return {"pdf_url": None, "landing_url": None, "oa_status": None, "error": None}
    url = unpaywall_url(bare, CONTACT_EMAIL)

    async def run() -> str:
        response = await fetch_once(url)
        # 404 表示该 DOI 没有开放获取记录，这是正常结论而非服务故障
        if response.status_code == 404:
            return json.dumps(
                {"pdf_url": None, "pdf_urls": [], "landing_url": None, "oa_status": None}
            )
        if response.status_code >= 400:
            raise FriendlyError(
                f"免费全文查询服务暂时不可用（{response.status_code}），请稍后重试。",
                response.status_code in {429, 500, 502, 503, 504},
            )
        payload = response.json()
        best = payload.get("best_oa_location") or {}
        # 出版商经常挡掉非浏览器请求（403），多留几个候选（例如 PMC 镜像）能显著提高导入成功率。
        pdf_urls: list[str] = []
        best_pdf = best.get("url_for_pdf")
        if best_pdf:
            pdf_urls.append(best_pdf)
        for location in payload.get("oa_locations") or []:
            candidate = location.get("url_for_pdf")
            if candidate and candidate not in pdf_urls:
                pdf_urls.append(candidate)
        return json.dumps(
            {
                # url_for_pdf 才是可直接下载的 PDF；url 往往只是落地页
                "pdf_url": best_pdf or (pdf_urls[0] if pdf_urls else None),
                "pdf_urls": pdf_urls,
                "landing_url": best.get("url") or None,
                "oa_status": payload.get("oa_status"),
            }
        )

    data = json.loads(await cache.get(url, TTL_UNPAYWALL, run))
    return {**data, "error": None}


async def resolve_pdf_urls(paper: Paper) -> list[str]:
    """详情页「导入文献库」用：题录自带 PDF 优先，其次 Unpaywall 的多个开放获取位置。"""
    candidates: list[str] = []

    def add(url: str | None) -> None:
        if url and url not in candidates:
            candidates.append(url)

    add(paper.pdf_url)
    if paper.doi:
        try:
            found = await find_free_full_text(paper.doi)
        except FriendlyError as exc:
            log.info("Unpaywall 查询失败（%s）：%s", paper.doi, exc.message)
            found = {}
        for url in found.get("pdf_urls") or []:
            add(url)
        add(found.get("pdf_url"))
    return candidates


async def fetch_pdf(url: str) -> bytes:
    if not safe_public_url(url):
        raise FriendlyError("该链接不是可下载的公开地址，请到原文页面手动下载。", False)
    data = await fetch_bytes(url)
    if not looks_like_pdf(data):
        raise FriendlyError(
            "下载到的内容不是 PDF（可能是出版商的访问验证页），请点「原文页」手动下载。", False
        )
    return data


# ── 中文查询译英 ────────────────────────────────────────────

_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)

# Google 两个免密钥端点的返回结构不同，两种都要认。
_TRANSLATE_ENDPOINTS = (
    "https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=en&q={}",
    "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t&q={}",
)


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def parse_google_translation(payload: object) -> str:
    """兼容两种响应：
    dict-chrome-ex —— [["Hello","zh-CN"]]
    gtx            —— [[["Hello","你好",null,null,10]],null,"zh-CN",...]
    """
    if not isinstance(payload, list) or not payload:
        return ""
    first = payload[0]
    if not isinstance(first, list):
        return ""
    segments = payload if first and isinstance(first[0], str) else first
    parts = []
    for seg in segments:
        if isinstance(seg, list) and seg and isinstance(seg[0], str):
            parts.append(seg[0])
    return "".join(parts).strip()


async def to_english_query(q: str) -> tuple[str, bool]:
    """各数据源对中文查询几乎无命中，命中 CJK 时先译成英文。失败则退回原查询。"""
    if not has_cjk(q):
        return q, False
    encoded = quote(q, safe="")
    for template in _TRANSLATE_ENDPOINTS:
        url = template.format(encoded)
        try:
            payload = json.loads(
                await fetch_text(url, timeout=8, retries=1, ttl=TTL_TRANSLATE)
            )
        except Exception:
            continue
        english = parse_google_translation(payload)
        if english:
            return english, True
    return q, False


# ── 文件名 ──────────────────────────────────────────────────


def pdf_filename(paper: Paper) -> str:
    bare = re.sub(r"^(arxiv|openalex|pubmed|europepmc):", "", paper.id)
    slug = "-".join((paper.title or "").split()[:6])
    slug = re.sub(r"[^\w\u4e00-\u9fff-]", "", slug)[:60]
    safe_id = re.sub(r"[^\w.-]", "_", bare)
    return f"{slug or 'paper'}-{safe_id}.pdf"
