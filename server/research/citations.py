"""综述引用审计：把“有没有引用”变成可验证的指标。

模型自评“已经核对过”不可靠，所以在服务端做确定性检查：
- 引用解析：`paperId + pN`、DOI；
- 页码校验：该页是否真的存在于解析后的全文；
- 支持度启发式：论断句与所引页文本的词元重合度，低重合只提示不阻断；
- 结构指标：页码级引用率、来源数、分节覆盖、无引用论断。

这里的检查不追求学术意义上的“蕴含判断”，而是用低成本信号把明显的
“编页码 / 引了但没读 / 整节只有一篇来源”筛出来。
"""

from __future__ import annotations

import re
from typing import Any

from server.research.search import STOPWORDS

LOCAL_RE = re.compile(r"p_[0-9a-f]{12}")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s，,；;）)\]】]+")
PAGE_RE = re.compile(r"(?:p|P|第)\s*(\d{1,3})\s*页?")
HEADING_RE = re.compile(r"^\s*#{2,4}\s+(.+)$")
ASCII_TOKEN_RE = re.compile(r"[a-z][a-z0-9\-+]{2,}")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
ASSERTION_RE = re.compile(
    r"表明|显示|证明|提示|导致|提高|降低|增加|减少|显著|优于|能够|可以|具有|"
    r"found|show|shows|shown|suggest|suggests|demonstrate|demonstrates|"
    r"increase|increases|reduce|reduces|improve|improves|associated|"
    r"significantly|effective|efficacy",
    re.IGNORECASE,
)
REFERENCE_TITLES = ("参考文献", "references", "bibliography", "引用文献")


def _page_after(line: str, end: int) -> int | None:
    window = line[end : end + 18]
    match = PAGE_RE.search(window)
    return int(match.group(1)) if match else None


def _page_before(line: str, start: int) -> int | None:
    window = line[max(0, start - 14) : start]
    found = list(PAGE_RE.finditer(window))
    return int(found[-1].group(1)) if found else None


def _local_citations(line: str) -> list[tuple[str, int | None]]:
    found: list[tuple[str, int | None]] = []
    for match in LOCAL_RE.finditer(line):
        page = _page_after(line, match.end())
        if page is None:
            page = _page_before(line, match.start())
        found.append((match.group(0), page))
    return found


def _split_sections(text: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = [{"title": "正文", "line": 1, "lines": []}]
    for number, line in enumerate(text.splitlines(), 1):
        heading = HEADING_RE.match(line)
        if heading:
            sections.append({"title": heading.group(1).strip(), "line": number, "lines": []})
        else:
            sections[-1]["lines"].append((number, line))
    return sections


def _is_reference(title: str) -> bool:
    low = title.lower()
    return any(key in low for key in REFERENCE_TITLES)


def _claim_sentences(number: int, line: str) -> list[tuple[int, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith(("|", ">", "#", "```")):
        return []
    parts = re.split(r"(?<=[。！？；;])\s*|(?<=[.!?])\s+", stripped)
    claims: list[tuple[int, str]] = []
    for part in parts:
        sentence = part.strip(" -*\t")
        if len(sentence) < 24:
            continue
        if not ASSERTION_RE.search(sentence):
            continue
        claims.append((number, sentence))
    return claims


def _ascii_tokens(text: str) -> set[str]:
    return {
        token
        for token in ASCII_TOKEN_RE.findall(text.lower())
        if token not in STOPWORDS
    }


def _cjk_bigrams(text: str) -> set[str]:
    cjk = "".join(CJK_RE.findall(text))
    return {cjk[index : index + 2] for index in range(len(cjk) - 1)}


def _support(claim: str, page_text: str) -> float:
    """论断与所引页的粗略重合度。

    中文论断引英文论文时，逐词重合没有意义；只有论断里保留了足够多的英文
    术语时才做英文比较。中文页则用二元字组比较。
    """
    claim_ascii = _ascii_tokens(claim)
    page_ascii = _ascii_tokens(page_text)
    if len(claim_ascii) >= 4 and page_ascii:
        ratio = len(claim_ascii & page_ascii) / len(claim_ascii)
    elif CJK_RE.search(claim) and CJK_RE.search(page_text):
        claim_grams = _cjk_bigrams(claim)
        if len(claim_grams) < 4:
            return 1.0
        ratio = len(claim_grams & _cjk_bigrams(page_text)) / len(claim_grams)
    else:
        # 跨语言且没有可比的英文术语，直接跳过，避免把所有引用误判为弱支持。
        return 1.0
    return ratio


def audit_review(text: str, project_id: str, library: Any) -> dict[str, Any]:
    sections = _split_sections(text)
    issues: list[dict[str, Any]] = []
    page_cache: dict[str, tuple[set[int] | None, list[dict[str, Any]]]] = {}

    local_total = 0
    page_level = 0
    doi_total = 0
    local_papers: set[str] = set()
    papers_with_page: set[str] = set()
    dois: set[str] = set()
    weak_support = 0
    unresolved: set[str] = set()
    section_reports: list[dict[str, Any]] = []

    for section in sections:
        if _is_reference(str(section["title"])):
            continue
        section_citations = 0
        section_sources: set[str] = set()

        for number, line in section["lines"]:
            citations = _local_citations(line)
            dois_in_line = {
                match.group(0).rstrip(".") for match in DOI_RE.finditer(line)
            }
            section_citations += len(citations) + len(dois_in_line)
            for paper_id, page in citations:
                local_total += 1
                local_papers.add(paper_id)
                section_sources.add(paper_id)
                if page is not None:
                    page_level += 1
                    papers_with_page.add(paper_id)

                row = library.get(paper_id)
                if row is None:
                    unresolved.add(paper_id)
                    if len(issues) < 40:
                        issues.append(
                            {
                                "kind": "unresolved_paper",
                                "paperId": paper_id,
                                "line": number,
                                "detail": "论文不在本地库，无法核对页码。",
                            }
                        )
                    continue

                if page is None:
                    continue
                if paper_id not in page_cache:
                    blocks = library.read_blocks(paper_id)
                    page_cache[paper_id] = (
                        None if blocks is None else {int(block.get("page") or 1) for block in blocks},
                        blocks or [],
                    )
                pages, blocks = page_cache[paper_id]
                if pages is None:
                    if len(issues) < 40:
                        issues.append(
                            {
                                "kind": "page_unverifiable",
                                "paperId": paper_id,
                                "page": page,
                                "line": number,
                                "detail": "这篇论文没有解析全文，页码无法核对。",
                            }
                        )
                    continue
                if page not in pages:
                    if len(issues) < 40:
                        issues.append(
                            {
                                "kind": "page_out_of_range",
                                "paperId": paper_id,
                                "page": page,
                                "line": number,
                                "detail": f"全文没有第 {page} 页（共 {max(pages)} 页）。",
                            }
                        )
                    continue

                blocks = blocks or []
                page_text = " ".join(
                    str(block.get("text") or "")
                    for block in blocks
                    if int(block.get("page") or 0) == page
                )
                claim = next(
                    (
                        sentence
                        for _, sentence in _claim_sentences(number, line)
                        if paper_id in sentence
                    ),
                    line,
                )
                ratio = _support(claim, page_text)
                if ratio < 0.18:
                    weak_support += 1
                    if len(issues) < 40:
                        issues.append(
                            {
                                "kind": "weak_support",
                                "paperId": paper_id,
                                "page": page,
                                "line": number,
                                "overlap": round(ratio, 3),
                                "claim": claim[:120],
                                "detail": "论断与所引页文本重合度低，建议核对原句。",
                            }
                        )

            for doi in dois_in_line:
                doi_total += 1
                dois.add(doi)
                section_sources.add(doi)

            if not citations and not dois_in_line:
                for _line_no, sentence in _claim_sentences(number, line):
                    if len(issues) < 40:
                        issues.append(
                            {
                                "kind": "uncited_claim",
                                "line": number,
                                "claim": sentence[:120],
                                "detail": "含结论性表述但没有引用。",
                            }
                        )

        section_reports.append(
            {
                "title": section["title"],
                "line": section["line"],
                "citations": section_citations,
                "sources": len(section_sources),
            }
        )

    citations_total = local_total + doi_total
    sources_unique = len(local_papers | dois)
    metrics = {
        "citationsTotal": citations_total,
        "localCitations": local_total,
        "pageLevel": page_level,
        "doiCitations": doi_total,
        "pageRatio": round(page_level / citations_total, 3) if citations_total else 0.0,
        "sourcePageRatio": round(len(papers_with_page) / sources_unique, 3)
        if sources_unique
        else 0.0,
        "sourcesUnique": sources_unique,
        "fulltextSources": len(local_papers),
        "sourcesWithPage": len(papers_with_page),
        "unresolved": len(unresolved),
        "weakSupport": weak_support,
        "uncitedClaims": sum(1 for issue in issues if issue["kind"] == "uncited_claim"),
        "sections": len(section_reports),
        "weakSections": sum(1 for report in section_reports if report["sources"] < 2),
    }

    suggestions: list[str] = []
    if metrics["pageRatio"] < 0.6:
        suggestions.append(
            f"页码级引用率 {round(metrics['pageRatio'] * 100)}%，低于 60%："
            "关键论断应先 paper_read 再标注页码，外部摘要来源不要写页码。"
        )
    if unresolved:
        suggestions.append(
            "有引用指向本地库中不存在的论文：" + "、".join(sorted(unresolved)[:5])
        )
    if weak_support:
        suggestions.append(
            f"{weak_support} 处引用与所引页文本重合度低，建议逐条核对原句是否支持该论断。"
        )
    weak_sections = [report["title"] for report in section_reports if report["sources"] < 2]
    if weak_sections:
        suggestions.append(
            "以下章节来源少于 2 篇，证据面偏窄：" + "、".join(weak_sections[:6])
        )
    if len(local_papers) < 5:
        suggestions.append(
            f"本地全文来源只有 {len(local_papers)} 篇，建议先导入并精读至少 5 篇，"
            "否则综述会过度依赖外部摘要。"
        )
    if not suggestions:
        suggestions.append("引用结构达标：页码、来源与分节覆盖均未发现明显缺口。")

    return {
        "projectId": project_id,
        "metrics": metrics,
        "sections": section_reports,
        "issues": issues,
        "suggestions": suggestions,
    }
