"""引用格式与导出。

各数据源给的作者串形态不统一（arXiv / OpenAlex 多为「名 姓」，PubMed / Europe PMC 多为
「姓 首字母」），先用 `split_name` 归一成 (family, given)，再按各格式拼装。
全是纯函数，网络与剪贴板留在路由层。
"""

from __future__ import annotations

import re

from .models import Paper

CITATION_STYLES: list[tuple[str, str]] = [
    ("gb7714", "GB/T 7714"),
    ("apa", "APA"),
    ("mla", "MLA"),
    ("ieee", "IEEE"),
    ("bibtex", "BibTeX"),
    ("ris", "RIS"),
]

STYLE_KEYS = {key for key, _ in CITATION_STYLES}


def split_name(raw: str) -> tuple[str, str]:
    """「Smith, John」/「John Smith」/「Smith J」→ (family, given)。"""
    name = re.sub(r"\s+", " ", raw or "").strip()
    if not name:
        return "", ""
    if "," in name:
        family, _, given = name.partition(",")
        return family.strip(), given.strip()

    parts = name.split(" ")
    last = parts[-1]
    # 末段形如 J / JA / J.A.（长度不超过 3 个字母）时，判为「姓在前、名缩写在后」
    if len(parts) > 1 and re.fullmatch(r"[A-Z][A-Z.]{0,3}", last) and len(last.replace(".", "")) <= 3:
        return " ".join(parts[:-1]), last
    return last, " ".join(parts[:-1])


def _initials(given: str) -> str:
    tokens = [t for t in re.split(r"[\s.]+", given or "") if t]
    return " ".join(f"{t[0].upper()}." for t in tokens)


def _people(paper: Paper) -> list[tuple[str, str]]:
    return [n for n in (split_name(a) for a in paper.authors) if n[0] or n[1]]


def _link(paper: Paper) -> str:
    return f"https://doi.org/{paper.doi}" if paper.doi else paper.url


def _end(text: str) -> str:
    return text if text.endswith(".") else f"{text}."


def _bibtex_escape(text: str) -> str:
    s = re.sub(r"([&%$#_])", r"\\\1", text or "")
    return re.sub(r"[{}]", "", s)


def _gb7714(paper: Paper) -> str:
    people = _people(paper)

    def fmt(person: tuple[str, str]) -> str:
        family, given = person
        return f"{family.upper()} {_initials(given).replace('.', '')}".strip()

    head = ", ".join(fmt(p) for p in people[:3])
    authors = f"{head}, 等" if len(people) > 3 else head
    mark = "[EB/OL]" if paper.source == "arxiv" else "[J]"
    source = ", ".join(x for x in (paper.venue, str(paper.year) if paper.year else "") if x)
    tail = f"doi:{paper.doi}." if paper.doi else (f"{paper.url}." if paper.url else "")
    parts = [f"{authors}." if authors else "", f"{paper.title}{mark}.", f"{source}." if source else "", tail]
    return " ".join(p for p in parts if p)


def _apa(paper: Paper) -> str:
    people = _people(paper)

    def fmt(person: tuple[str, str]) -> str:
        family, given = person
        return f"{family}, {_initials(given)}".strip().rstrip(",")

    if len(people) == 1:
        authors = fmt(people[0])
    elif 1 < len(people) <= 3:
        authors = f"{', '.join(fmt(p) for p in people[:-1])}, & {fmt(people[-1])}"
    elif len(people) > 3:
        authors = f"{fmt(people[0])} et al."
    else:
        authors = ""

    year = f"({paper.year})." if paper.year else "(无日期)."
    parts = [
        _end(authors) if authors else "",
        year,
        f"{paper.title}.",
        f"{paper.venue}." if paper.venue else "",
        _link(paper),
    ]
    return " ".join(p for p in parts if p)


def _mla(paper: Paper) -> str:
    people = _people(paper)
    first = f"{people[0][0]}, {people[0][1]}".strip() if people else ""
    if len(people) == 2:
        authors = f"{first}, and {people[1][1]} {people[1][0]}".strip()
    elif len(people) > 2:
        authors = f"{first}, et al."
    else:
        authors = first
    parts = [
        f"{authors}." if authors else "",
        f'"{paper.title}."',
        f"{paper.venue}," if paper.venue else "",
        f"{paper.year}," if paper.year else "",
        _link(paper),
    ]
    return " ".join(p for p in parts if p)


def _ieee(paper: Paper) -> str:
    people = _people(paper)

    def fmt(person: tuple[str, str]) -> str:
        family, given = person
        return f"{_initials(given)} {family}".strip()

    if len(people) == 1:
        authors = fmt(people[0])
    elif len(people) == 2:
        authors = f"{fmt(people[0])} and {fmt(people[1])}"
    elif len(people) == 3:
        authors = f"{fmt(people[0])}, {fmt(people[1])}, and {fmt(people[2])}"
    elif len(people) > 3:
        authors = f"{fmt(people[0])} et al."
    else:
        authors = ""

    source = ", ".join(x for x in (paper.venue, str(paper.year) if paper.year else "") if x)
    head = f"{authors}, " if authors else ""
    parts = [
        f'{head}"{paper.title},"',
        f"{source}." if source else "",
        f"{_link(paper)}." if not source and _link(paper) else "",
    ]
    return " ".join(p for p in parts if p).strip()


def bibtex_key(paper: Paper) -> str:
    first = "anon"
    if paper.authors:
        tokens = [t for t in re.split(r"[\s,]+", paper.authors[0]) if t]
        first = tokens[-1] if tokens else "anon"
    word = paper.title.split()[0] if paper.title else ""
    key = re.sub(r"[^A-Za-z0-9]", "", f"{first}{paper.year or ''}{word}")
    return key or "reference"


def build_bibtex(papers: list[Paper]) -> str:
    entries = []
    for paper in papers:
        kind = "misc" if paper.source == "arxiv" else "article"
        lines = [f"@{kind}{{{bibtex_key(paper)},"]
        lines.append(f"  title        = {{{_bibtex_escape(paper.title)}}},")
        if paper.authors:
            authors = " and ".join(_bibtex_escape(a) for a in paper.authors)
            lines.append(f"  author       = {{{authors}}},")
        if paper.year:
            lines.append(f"  year         = {{{paper.year}}},")
        if paper.source == "arxiv":
            lines.append(f"  eprint       = {{{re.sub(r'^arxiv:', '', paper.id)}}},")
            lines.append("  archivePrefix= {arXiv},")
            if paper.category:
                lines.append(f"  primaryClass = {{{paper.category}}},")
        elif paper.venue:
            lines.append(f"  journal      = {{{_bibtex_escape(paper.venue)}}},")
        if paper.doi:
            lines.append(f"  doi          = {{{paper.doi}}},")
        lines.append(f"  url          = {{{paper.url}}},")
        lines.append("}")
        entries.append("\n".join(lines))
    return "\n\n".join(entries)


def build_ris(papers: list[Paper]) -> str:
    records = []
    for paper in papers:
        lines = [f"TY  - {'GEN' if paper.source == 'arxiv' else 'JOUR'}", f"TI  - {paper.title}"]
        for author in paper.authors:
            lines.append(f"AU  - {author}")
        if paper.year:
            lines.append(f"PY  - {paper.year}")
        if paper.venue:
            lines.append(f"JO  - {paper.venue}")
        if paper.category:
            lines.append(f"KW  - {paper.category}")
        if paper.doi:
            lines.append(f"DO  - {paper.doi}")
        if paper.abstract:
            lines.append(f"AB  - {paper.abstract}")
        lines.append(f"UR  - {paper.url}")
        lines.append("ER  - ")
        records.append("\n".join(lines))
    return "\n\n".join(records)


def format_citation(paper: Paper, style: str) -> str:
    if style == "gb7714":
        return _gb7714(paper)
    if style == "apa":
        return _apa(paper)
    if style == "mla":
        return _mla(paper)
    if style == "ieee":
        return _ieee(paper)
    if style == "bibtex":
        return build_bibtex([paper])
    if style == "ris":
        return build_ris([paper])
    return _gb7714(paper)
