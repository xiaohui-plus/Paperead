"""四个数据源的查询构造与结果解析。

每个源一套语法、一套返回结构，这里把差异全部吸收掉，上层只拿到 `Paper`。
解析函数不碰网络，方便用固定 fixture 做回归测试。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from .models import (
    ARXIV_EPOCH_YEAR,
    Paper,
    clean_html_text,
    decode_xml,
    epmc_key,
    epmc_pdf_url,
    normalize_year,
    reconstruct_abstract,
    split_epmc_key,
    strip_doi,
    strip_latex_pointers,
)

ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

FIELD_PREFIX = {"all": "all", "title": "ti", "abstract": "abs", "author": "au"}

ARXIV_API = "https://export.arxiv.org/api/query"
PUBMED_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENALEX_API = "https://api.openalex.org/works"
EPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1"
CORE_API = "https://api.core.ac.uk/v3"

# 只取渲染需要的字段，避免 OpenAlex 返回数十个无关字段。
OPENALEX_SELECT = ",".join(
    [
        "id",
        "doi",
        "title",
        "display_name",
        "publication_year",
        "publication_date",
        "cited_by_count",
        "abstract_inverted_index",
        "authorships",
        "primary_location",
        "primary_topic",
        "keywords",
        "best_oa_location",
    ]
)

# Semantic Scholar Graph API 返回字段很多，只取渲染与入库需要的。
S2_FIELDS = ",".join(
    [
        "paperId",
        "title",
        "abstract",
        "year",
        "publicationDate",
        "venue",
        "authors",
        "citationCount",
        "externalIds",
        "openAccessPdf",
        "fieldsOfStudy",
        "url",
    ]
)

# Europe PMC 只支持 cursorMark 游标翻页，改用「一次多取、本地切片」实现页码跳转。
EPMC_WINDOW = 200


# ── arXiv ───────────────────────────────────────────────────


def encode_phrase(text: str) -> str:
    """短语整体加引号，内部空格用 + 连接（arXiv 官方语法要求）。"""
    body = "+".join(quote(term, safe="") for term in text.strip().split())
    return f"%22{body}%22"


def build_arxiv_query(
    q: str,
    *,
    field: str = "all",
    phrase: bool = False,
    category: str | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    now_year: int | None = None,
) -> str:
    prefix = FIELD_PREFIX.get(field, "all")
    clauses: list[str] = []
    text = (q or "").strip()
    if text:
        if phrase:
            terms = [f"{prefix}:{encode_phrase(text)}"]
        else:
            terms = [f"{prefix}:{quote(term, safe='')}" for term in text.split()]
        # 长查询逐词 AND 在 arXiv 上几乎总是 0 命中；超过 3 个词时改用 OR，
        # 由 arXiv 的相关度排序挑回最贴近的结果。
        if len(terms) == 1:
            clauses.append(terms[0])
        elif len(terms) > 3:
            # OR 需要括号限定优先级；AND 保持原有不带括号的格式。
            clauses.append("(" + "+OR+".join(terms) + ")")
        else:
            clauses.append("+AND+".join(terms))

    cat = (category or "").strip()
    if cat:
        clauses.append(f"cat:{quote(cat, safe='')}")

    if from_year is not None or to_year is not None:
        start = from_year or ARXIV_EPOCH_YEAR
        end = to_year or now_year or datetime.now().year
        clauses.append(f"submittedDate:[{start}01010000+TO+{end}12312359]")

    return "+AND+".join(clauses)


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return decode_xml("".join(node.itertext()))


def parse_arxiv_entries(xml_text: str) -> list[Paper]:
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    papers: list[Paper] = []
    for entry in root.findall("a:entry", ATOM):
        raw_id = _text(entry.find("a:id", ATOM))
        raw_id = re.sub(r"^https?://arxiv\.org/abs/", "", raw_id)
        raw_id = re.sub(r"v\d+$", "", raw_id).strip()
        if not raw_id:
            continue

        cats = [
            c.get("term") or ""
            for c in entry.findall("a:category", ATOM)
            if c.get("term")
        ]
        primary = entry.find("arxiv:primary_category", ATOM)
        category = (primary.get("term") if primary is not None else None) or (
            cats[0] if cats else "arXiv"
        )

        pdf_url = f"https://arxiv.org/pdf/{raw_id}"
        for link in entry.findall("a:link", ATOM):
            if link.get("title") == "pdf" and link.get("href"):
                pdf_url = link.get("href") or pdf_url
                break

        authors = [_text(n).strip() for n in entry.findall("a:author/a:name", ATOM)]
        title = strip_latex_pointers(_text(entry.find("a:title", ATOM)))
        abstract = strip_latex_pointers(_text(entry.find("a:summary", ATOM)))
        published = _text(entry.find("a:published", ATOM)) or None

        papers.append(
            Paper(
                id=f"arxiv:{raw_id}",
                source="arxiv",
                title=re.sub(r"\s+", " ", title).strip(),
                authors=[a for a in authors if a],
                venue=_text(entry.find("arxiv:journal_ref", ATOM)) or "arXiv 预印本",
                year=normalize_year(published),
                category=category,
                keywords=[c for c in cats if c],
                abstract=abstract,
                url=f"https://arxiv.org/abs/{raw_id}",
                pdf_url=pdf_url,
                html_url=f"https://arxiv.org/html/{raw_id}",
                published_at=published,
                doi=_text(entry.find("arxiv:doi", ATOM)) or None,
                citations=None,
            )
        )
    return papers


# ── OpenAlex ────────────────────────────────────────────────


def parse_openalex_works(payload: dict[str, Any] | None) -> list[Paper]:
    results = (payload or {}).get("results") or []
    papers: list[Paper] = []
    for item in results:
        if not item:
            continue
        work_id = re.sub(r"^https?://openalex\.org/", "", item.get("id") or "")
        doi = strip_doi(item.get("doi"))
        landing = (item.get("primary_location") or {}).get("landing_page_url")
        topic = item.get("primary_topic") or {}
        venue = (item.get("primary_location") or {}).get("source") or {}
        keywords = [
            k.get("display_name") or ""
            for k in (item.get("keywords") or [])
            if k.get("display_name")
        ]
        papers.append(
            Paper(
                id=f"openalex:{work_id}",
                source="openalex",
                title=strip_latex_pointers(
                    item.get("title") or item.get("display_name") or "(无标题)"
                ),
                authors=[
                    a.get("author", {}).get("display_name") or ""
                    for a in (item.get("authorships") or [])
                    if a.get("author")
                ],
                venue=venue.get("display_name") or "OpenAlex",
                year=item.get("publication_year"),
                category=(topic.get("field") or {}).get("display_name")
                or topic.get("display_name")
                or "OpenAlex",
                keywords=keywords[:8],
                abstract=strip_latex_pointers(
                    reconstruct_abstract(item.get("abstract_inverted_index"))
                ),
                url=landing
                or (f"https://doi.org/{doi}" if doi else f"https://openalex.org/{work_id}"),
                # 只认真正的 PDF 直链；OA 落地页交给 Unpaywall 去解析
                pdf_url=(item.get("best_oa_location") or {}).get("pdf_url") or None,
                html_url=None,
                published_at=item.get("publication_date"),
                doi=doi,
                citations=item.get("cited_by_count"),
            )
        )
    return papers


# ── Semantic Scholar ────────────────────────────────────────

ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)


def _arxiv_pdf_from_ids(external: dict[str, Any], doi: str | None) -> str | None:
    """S2 经常不给 arXiv 预印本的 openAccessPdf，但题录里有 arXiv 号可推 PDF。"""
    arxiv_id = str(external.get("ArXiv") or "").strip()
    if not arxiv_id and doi:
        match = ARXIV_DOI_RE.match(doi)
        if match:
            arxiv_id = match.group(1).strip()
    if not arxiv_id:
        return None
    return f"https://arxiv.org/pdf/{arxiv_id}"


def parse_semantic_scholar(payload: dict[str, Any] | None) -> list[Paper]:
    results = (payload or {}).get("data") or []
    papers: list[Paper] = []
    for item in results:
        if not item:
            continue
        paper_id = item.get("paperId") or ""
        if not paper_id:
            continue
        external = item.get("externalIds") or {}
        fields = [f for f in (item.get("fieldsOfStudy") or []) if f]
        open_access = item.get("openAccessPdf") or {}
        doi = strip_doi(external.get("DOI"))
        papers.append(
            Paper(
                id=f"semanticscholar:{paper_id}",
                source="semanticscholar",
                title=strip_latex_pointers(item.get("title") or "(无标题)"),
                authors=[
                    a.get("name") or ""
                    for a in (item.get("authors") or [])
                    if a.get("name")
                ],
                venue=item.get("venue") or "Semantic Scholar",
                year=normalize_year(item.get("year")),
                category=fields[0] if fields else "Semantic Scholar",
                keywords=fields[:8],
                abstract=strip_latex_pointers((item.get("abstract") or "").strip()),
                url=item.get("url")
                or f"https://www.semanticscholar.org/paper/{paper_id}",
                pdf_url=open_access.get("url") or _arxiv_pdf_from_ids(external, doi),
                html_url=None,
                published_at=item.get("publicationDate"),
                doi=doi,
                citations=item.get("citationCount"),
            )
        )
    return papers


# ── CORE ────────────────────────────────────────────────────


def _core_topics(item: dict[str, Any]) -> list[str]:
    return [
        t.get("name") or ""
        for t in (item.get("topics") or [])
        if isinstance(t, dict) and t.get("name")
    ]


def _core_venue(item: dict[str, Any]) -> str:
    for journal in item.get("journals") or []:
        if isinstance(journal, dict) and journal.get("title"):
            return str(journal["title"])
    return str(item.get("publisher") or "CORE")


def parse_core(payload: dict[str, Any] | None) -> list[Paper]:
    results = (payload or {}).get("results") or []
    papers: list[Paper] = []
    for item in results:
        if not item:
            continue
        raw_id = item.get("id")
        if raw_id is None:
            continue
        authors = [
            a.get("name") if isinstance(a, dict) else str(a)
            for a in (item.get("authors") or [])
        ]
        topics = _core_topics(item)
        field_of_study = item.get("fieldOfStudy")
        if isinstance(field_of_study, list):
            field_of_study = field_of_study[0] if field_of_study else None
        papers.append(
            Paper(
                id=f"core:{raw_id}",
                source="core",
                title=strip_latex_pointers(item.get("title") or "(无标题)"),
                authors=[a for a in authors if a],
                venue=_core_venue(item),
                year=normalize_year(item.get("yearPublished") or item.get("publishedDate")),
                category=field_of_study or (topics[0] if topics else "CORE"),
                keywords=topics[:8],
                abstract=clean_html_text(item.get("abstract") or ""),
                url=f"https://core.ac.uk/works/{raw_id}",
                pdf_url=item.get("downloadUrl") or None,
                html_url=None,
                published_at=item.get("publishedDate"),
                doi=strip_doi(item.get("doi")),
                citations=item.get("citationCount"),
            )
        )
    return papers


# ── Europe PMC ──────────────────────────────────────────────


def parse_europe_pmc(payload: dict[str, Any] | None) -> list[Paper]:
    results = ((payload or {}).get("resultList") or {}).get("result") or []
    papers: list[Paper] = []
    for record in results:
        if not record:
            continue
        src = record.get("source") or "MED"
        record_id = record.get("id") or record.get("pmid") or record.get("pmcid") or ""
        if not record_id:
            continue
        journal = (record.get("journalInfo") or {}).get("journal") or {}
        mesh = [
            m.get("descriptorName") or ""
            for m in ((record.get("meshHeadingList") or {}).get("meshHeading") or [])
        ]
        keywords = (record.get("keywordList") or {}).get("keyword") or mesh[:8]
        pub_types = (record.get("pubTypeList") or {}).get("pubType") or []
        papers.append(
            Paper(
                id=epmc_key(src, record_id),
                source="europepmc",
                title=strip_latex_pointers((record.get("title") or "").strip() or "(无标题)"),
                authors=[
                    a.strip()
                    for a in re.split(r",\s*", record.get("authorString") or "")
                    if a.strip()
                ],
                venue=journal.get("title") or "Europe PMC",
                year=normalize_year(record.get("pubYear")),
                category=(mesh[0] if mesh else (pub_types[0] if pub_types else "Europe PMC")),
                keywords=[k for k in keywords if k][:8],
                abstract=clean_html_text(record.get("abstractText") or ""),
                url=f"https://europepmc.org/article/{src}/{record_id}",
                pdf_url=epmc_pdf_url(record),
                html_url=None,
                published_at=record.get("firstPublicationDate"),
                doi=record.get("doi"),
                citations=record.get("citedByCount"),
            )
        )
    return papers


# ── PubMed ──────────────────────────────────────────────────


def parse_pubmed_summary(
    summary: dict[str, Any], abstract: str = "", keywords: list[str] | None = None
) -> Paper:
    article_ids = {a.get("idtype"): a.get("value") for a in (summary.get("articleids") or [])}
    doi = article_ids.get("doi")
    uid = str(summary.get("uid") or "")
    pub_types = summary.get("pubtype") or []
    return Paper(
        id=f"pubmed:{uid}",
        source="pubmed",
        title=strip_latex_pointers(decode_xml(summary.get("title") or "")),
        authors=[a.get("name") or "" for a in (summary.get("authors") or [])],
        venue=summary.get("fulljournalname") or summary.get("source") or "PubMed",
        year=normalize_year(summary.get("pubdate")),
        category=pub_types[0] if pub_types else "PubMed",
        keywords=[k for k in (keywords or []) if k][:8],
        abstract=abstract,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
        # PMC 的 landing page 是 HTML，不是 PDF；这里不冒充全文直链，
        # 导入时由 Unpaywall 按 DOI 找真正的开放获取 PDF。
        pdf_url=None,
        html_url=None,
        published_at=None,
        doi=doi,
        citations=None,
    )


def parse_pubmed_abstract_xml(xml_text: str) -> tuple[str, list[str]]:
    """efetch 的 XML → (带小标题分节的摘要, 关键词 / MeSH)。"""
    if not xml_text:
        return "", []
    sections: list[str] = []
    for match in re.finditer(
        r"<AbstractText([^>]*)>([\s\S]*?)</AbstractText>", xml_text
    ):
        label = _label(match.group(1))
        body = clean_html_text(decode_xml(match.group(2)))
        sections.append(f"{label}: {body}" if label else body)
    abstract = "\n\n".join(s for s in sections if s)
    keywords = [decode_xml(m) for m in re.findall(r"<Keyword[^>]*>([\s\S]*?)</Keyword>", xml_text)]
    if not keywords:
        keywords = [
            decode_xml(m)
            for m in re.findall(r"<DescriptorName[^>]*>([\s\S]*?)</DescriptorName>", xml_text)
        ][:8]
    return abstract, [k for k in keywords if k]


def _label(attrs: str) -> str:
    match = re.search(r'Label="([^"]+)"', attrs or "")
    return match.group(1) if match else ""


# efetch 一次返回多条记录，按文章块切开才能把摘要对回各自的 PMID。
_PUBMED_ARTICLE_RE = re.compile(
    r"<(PubmedArticle|PubmedBookArticle)\b[\s\S]*?</\1>"
)


def parse_pubmed_abstracts(xml_text: str) -> dict[str, tuple[str, list[str]]]:
    """efetch 批量 XML → {PMID: (摘要, 关键词)}，供列表页按 PMID 回填。"""
    if not xml_text:
        return {}
    found: dict[str, tuple[str, list[str]]] = {}
    for match in _PUBMED_ARTICLE_RE.finditer(xml_text):
        block = match.group(0)
        pmid = re.search(r"<PMID[^>]*>(\d+)</PMID>", block)
        if pmid:
            found[pmid.group(1)] = parse_pubmed_abstract_xml(block)
    return found


# ── 查询参数拼装 ────────────────────────────────────────────


def arxiv_url(
    q: str,
    *,
    field: str,
    phrase: bool,
    category: str | None,
    from_year: int | None,
    to_year: int | None,
    sort: str,
    page: int,
    per_page: int,
) -> str:
    search_query = build_arxiv_query(
        q,
        field=field,
        phrase=phrase,
        category=category,
        from_year=from_year,
        to_year=to_year,
    )
    sort_by = {"new": "submittedDate", "updated": "lastUpdatedDate"}.get(sort, "relevance")
    return (
        f"{ARXIV_API}?search_query={search_query}"
        f"&start={page * per_page}&max_results={per_page}"
        f"&sortBy={sort_by}&sortOrder=descending"
    )


def openalex_url(
    q: str,
    *,
    field: str,
    phrase: bool,
    from_year: int | None,
    to_year: int | None,
    sort: str,
    page: int,
    per_page: int,
    mailto: str,
) -> str:
    params: list[tuple[str, str]] = []
    filters: list[str] = []
    limited = sanitize_filter_value(q)

    if field != "all" and limited:
        key = {"title": "title", "abstract": "abstract"}.get(field, "raw_author_name")
        filters.append(f"{key}.search:{limited}")
    else:
        params.append(("search", f'"{q}"' if phrase else q))

    if from_year is not None:
        filters.append(f"from_publication_date:{from_year}-01-01")
    if to_year is not None:
        filters.append(f"to_publication_date:{to_year}-12-31")
    if sort != "rel":
        params.append(("sort", "publication_date:desc"))
    params.extend(
        [
            ("per-page", str(per_page)),
            ("page", str(page + 1)),
            ("select", OPENALEX_SELECT),
            ("mailto", mailto),
        ]
    )
    if filters:
        params.append(("filter", ",".join(filters)))
    query = "&".join(f"{k}={quote(v, safe=',:|().{}')}" for k, v in params)
    return f"{OPENALEX_API}?{query}"


def sanitize_filter_value(text: str) -> str:
    """OpenAlex 的 filter 用逗号/冒号分隔，检索词里出现会破坏结构。"""
    return re.sub(r"\s+", " ", re.sub(r"[,:]", " ", text or "")).strip()


def europe_pmc_url(
    q: str,
    *,
    field: str,
    phrase: bool,
    from_year: int | None,
    to_year: int | None,
    sort: str,
    page: int,
    per_page: int,
    now_year: int | None = None,
) -> tuple[str, int]:
    term = f'"{q}"' if phrase else q
    clauses: list[str] = []
    if field == "title":
        clauses.append(f"TITLE:{term}")
    elif field == "abstract":
        clauses.append(f"ABSTRACT:{term}")
    elif field == "author":
        clauses.append(f"AUTH:{term}")
    else:
        clauses.append(term)

    if from_year is not None or to_year is not None:
        start = from_year or ARXIV_EPOCH_YEAR
        end = to_year or now_year or datetime.now().year
        clauses.append(f"FIRST_PDATE:[{start}-01-01 TO {end}-12-31]")

    page_limit = max(1, EPMC_WINDOW // per_page)
    page = min(page, page_limit - 1)
    offset = page * per_page
    size = min(offset + per_page, EPMC_WINDOW)
    params = [
        ("query", " AND ".join(clauses)),
        ("format", "json"),
        ("resultType", "core"),
        ("pageSize", str(size)),
    ]
    if sort != "rel":
        params.append(("sort", "P_PDATE_D desc"))
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
    return f"{EPMC_API}?{query}", page_limit


def pubmed_urls(
    q: str,
    *,
    field: str,
    phrase: bool,
    from_year: int | None,
    to_year: int | None,
    sort: str,
    page: int,
    per_page: int,
    now_year: int | None = None,
) -> str:
    size = per_page
    text = f'"{q}"' if phrase else q
    tag = {"title": "ti", "abstract": "ab", "author": "au"}.get(field, "")
    term = f"{text}[{tag}]" if tag else text
    if from_year is not None or to_year is not None:
        start = from_year or ARXIV_EPOCH_YEAR
        end = to_year or now_year or datetime.now().year
        term += f' AND ("{start}"[dp] : "{end}"[dp])'
    sort_param = "relevance" if sort == "rel" else "pub_date"
    return (
        f"{PUBMED_EUTILS}/esearch.fcgi?db=pubmed&retmode=json"
        f"&retmax={size}&retstart={page * size}&sort={sort_param}"
        f"&term={quote(term, safe='')}"
    )


def semantic_scholar_url(
    q: str,
    *,
    phrase: bool,
    from_year: int | None,
    to_year: int | None,
    page: int,
    per_page: int,
    now_year: int | None = None,
) -> str:
    """Semantic Scholar 的关键词检索只支持相关度排序，没有字段限定与自定义排序。"""
    params: list[tuple[str, str]] = [
        ("query", f'"{q}"' if phrase else q),
        ("offset", str(page * per_page)),
        ("limit", str(min(per_page, 100))),
        ("fields", S2_FIELDS),
    ]
    if from_year is not None or to_year is not None:
        start = from_year or ARXIV_EPOCH_YEAR
        end = to_year or now_year or datetime.now().year
        params.append(("year", f"{start}-{end}"))
    return f"{SEMANTIC_SCHOLAR_API}/paper/search?{urlencode(params)}"


def core_url(
    q: str,
    *,
    field: str,
    phrase: bool,
    from_year: int | None,
    to_year: int | None,
    page: int,
    per_page: int,
) -> str:
    """CORE v3 使用 Elasticsearch 查询串；年份用 range 语法收窄。"""
    term = re.sub(r'"', " ", q or "").strip()
    text = f'"{term}"' if phrase else term
    key = {"title": "title", "abstract": "abstract", "author": "authors"}.get(field)
    clauses = [f"{key}:({text})" if key else text]

    if from_year is not None or to_year is not None:
        start = str(from_year) if from_year is not None else "*"
        end = str(to_year) if to_year is not None else "*"
        clauses.append(f"yearPublished:[{start} TO {end}]")

    params = [
        ("q", " AND ".join(clauses)),
        ("limit", str(min(per_page, 100))),
        ("offset", str(page * per_page)),
    ]
    return f"{CORE_API}/search/works?{urlencode(params)}"


def core_doi_query(doi: str) -> str:
    """CORE 的自定义检索串里，冒号与引号会破坏语法，先清洗序号。"""
    safe = re.sub(r'["\\]', " ", doi or "").strip()
    return f'doi:"{safe}"'


def core_doi_url(doi: str) -> str:
    return f"{CORE_API}/search/works?{urlencode([('q', core_doi_query(doi)), ('limit', '1')])}"


def unpaywall_url(doi: str, mailto: str) -> str:
    return f"{UNPAYWALL_API}/{quote(doi, safe='')}?email={quote(mailto, safe='')}"


def epmc_detail_query(paper_id: str) -> str | None:
    """`europepmc:PMC/PMC123` → 检索式。"""
    key = split_epmc_key(paper_id.split(":", 1)[-1])
    if not key:
        return None
    src, record_id = key
    if src == "PMC":
        return f"PMCID:{record_id}"
    return f"SRC:{src} AND EXT_ID:{record_id}"
