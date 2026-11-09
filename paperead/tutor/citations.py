"""从正文里找出引用标记，并映射到参考文献条目。

支持三种主流写法：
- 数字：`[31]` / `[12,13]` / `[3-5]`
- 作者-年份（带括号）：`(Kipf and Welling 2016a)` / `(Zhang et al. 2018)`
- 作者-年份（不带括号，AAAI 等）：`Kipf and Welling 2016` / `Velikovi et al. 2017`

上标数字（Nature/Science 常见）在文本里会被压平，无法可靠区分，
由 lineage 模块借助 PDF 字体信息单独提取。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .references import Reference

_NUM_RE = re.compile(r"\[(\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*)\]")
# 少数期刊直接写 `(1,2)` / `(8)` 而不是 `[1,2]`
_PAREN_NUM_RE = re.compile(r"\((\d{1,2}(?:\s*[,\-–]\s*\d{1,2})*)\)")

# 正文里的年份：1960–2049，避免把 2024 这类年份再拆成别的
_YEAR_RE = re.compile(r"\b((?:19[6-9]|20[0-4])\d)[a-z]?\b")
_SURNAME = r"[A-Z][A-Za-z\u2019'\-]{1,24}"
# 年份前面紧挨着的作者串（只取最后一个姓，足够做消歧）
_AUTHOR_TAIL_RE = re.compile(
    rf"({_SURNAME})(?:\s+et\s+al\.?|\s+and\s+{_SURNAME}|"
    rf"(?:\s*,\s*{_SURNAME})*(?:\s*,?\s*(?:and|&)\s+{_SURNAME})?)?\s*,?\s*$"
)
_STOP_SURNAMES = {
    "in", "the", "this", "that", "these", "those", "since", "from", "during",
    "by", "between", "after", "before", "around", "until", "within", "of", "on",
    "at", "for", "and", "or", "we", "they", "it", "its", "their", "our",
    "figure", "fig", "table", "section", "eq", "equation", "appendix", "chapter",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


@dataclass
class Citation:
    marker: str
    index: int | None = None
    surname: str | None = None
    year: int | None = None
    start: int = 0
    end: int = 0


def _expand(nums: str) -> list[int]:
    out: list[int] = []
    for part in nums.split(","):
        part = part.strip()
        m = re.match(r"^(\d+)\s*[\-–]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < b - a <= 20:
                out.extend(range(a, b + 1))
            else:
                out.extend([a, b])
        elif part.isdigit():
            out.append(int(part))
    return out


def _ascii(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).lower()


def _author_zone(raw: str) -> str:
    """参考文献开头属于作者的那一段。作者-年份在开头，IEEE 在后面。"""
    raw = re.sub(r"^\s*\[?\d{1,3}\]?[.)]?\s*", "", raw)
    m = _YEAR_RE.search(raw)
    if m and m.start() > 10:
        return raw[: m.start()]
    return raw[:180]


def _name_match(surname: str, raw: str) -> bool:
    s = _ascii(surname)
    zone = _ascii(_author_zone(raw))
    if not s or not zone:
        return False
    if s in zone:
        return True
    # 拼写/变音符号差异：Velikovi vs Veličković
    return len(s) >= 5 and s[:5] in zone


def find(text: str) -> list[Citation]:
    cites: list[Citation] = []
    for m in _NUM_RE.finditer(text):
        for n in _expand(m.group(1)):
            cites.append(Citation(marker=m.group(0), index=n, start=m.start(), end=m.end()))

    # 作者-年份：扫描每个年份，往前找最近的姓
    for m in _YEAR_RE.finditer(text):
        pre = text[max(0, m.start() - 120): m.start()]
        tail = _AUTHOR_TAIL_RE.search(pre)
        if not tail:
            continue
        surname = tail.group(1)
        if surname.lower() in _STOP_SURNAMES:
            continue
        start = m.start() - len(tail.group(0))
        cites.append(
            Citation(
                marker=text[start:m.end()].strip(),
                surname=surname,
                year=int(m.group(1)),
                start=max(0, start),
                end=m.end(),
            )
        )

    cites.sort(key=lambda c: (c.start, c.end))
    return cites


def find_paren_numeric(text: str) -> list[Citation]:
    """识别 `(1,2)` / `(8)` 这类圆括号数字引用。

    只在论文明显采用这种编号格式时启用，否则容易把公式编号、图号误当成引用。
    """
    cites: list[Citation] = []
    for m in _PAREN_NUM_RE.finditer(text):
        for n in _expand(m.group(1)):
            cites.append(
                Citation(marker=m.group(0), index=n, start=m.start(), end=m.end())
            )
    return cites


def match_index(cite: Citation, references: list[Reference]) -> int | None:
    """把一个引用标记落到具体参考文献上；落不到就返回 None。"""
    if cite.index is not None:
        for r in references:
            if r.index == cite.index:
                return r.index
        return None
    if cite.surname and cite.year:
        for r in references:
            if not r.year or str(cite.year) not in r.raw:
                continue
            if _name_match(cite.surname, r.raw):
                return r.index
    return None


def resolve(cites: list[Citation], references: list[Reference]) -> list[dict]:
    """把标记解析成具体文献；解析不到的直接丢掉（宁缺毋滥）。"""
    by_index = {r.index: r for r in references}
    out: list[dict] = []
    seen: set[int] = set()

    for c in cites:
        idx = match_index(c, references)
        ref = by_index.get(idx) if idx is not None else None
        if not ref or ref.index in seen:
            continue
        seen.add(ref.index)
        out.append({"marker": c.marker, "ref": ref.to_dict()})
    return out
