"""从正文里找出引用标记，并映射到参考文献条目。

支持三种主流写法：
- 数字：`[31]` / `[12,13]` / `[3-5]`
- 作者-年份（带括号）：`(Kipf and Welling 2016a)` / `(Zhang et al. 2018)`
- 作者-年份（不带括号，AAAI 等）：`Kipf and Welling 2016` / `Velikovi et al. 2017`

作者-年份支持任意数量的作者：`(Louis, Jacob, and Salehi-Abari 2023)`。
映射时以「首作者 + 年份 + 年份后缀」为主，避免同姓/共同作者串条。

上标数字（Nature/Science 常见）在文本里会被压平，无法可靠区分，
由 lineage 模块借助 PDF 字体信息单独提取。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .references import Reference

_NUM_RE = re.compile(r"\[(\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*)\]")
# 少数期刊直接写 `(1,2)` / `(8)` 而不是 `[1,2]`
_PAREN_NUM_RE = re.compile(r"\((\d{1,2}(?:\s*[,\-–]\s*\d{1,2})*)\)")

# 正文里的年份：1960–2049，避免把 2024 这类年份再拆成别的。
# 第二组是年份后缀（`2016a` / `2016b`），同作者同年多篇靠它区分。
_YEAR_RE = re.compile(r"\b((?:19[6-9]|20[0-4])\d)([a-z])?\b")
_SURNAME = r"[A-Z][A-Za-z\u2019'\-]{1,24}"
# 作者之间的连接：`A, B` / `A, and B` / `A and B` / `A & B`
_NAME_JOIN = r"(?:\s*,\s*(?:and\s+|&\s+)?|\s+and\s+|\s+&\s+)"
# 年份前面紧挨着的作者串（任意作者数，末尾允许 `et al.`）
_AUTHOR_TAIL_RE = re.compile(
    rf"({_SURNAME}(?:{_NAME_JOIN}{_SURNAME})*(?:\s+et\s+al\.?)?)\s*,?\s*$"
)
_STOP_SURNAMES = {
    "in", "the", "this", "that", "these", "those", "since", "from", "during",
    "by", "between", "after", "before", "around", "until", "within", "of", "on",
    "at", "for", "and", "or", "we", "they", "it", "its", "their", "our",
    "figure", "fig", "table", "section", "eq", "equation", "appendix", "chapter",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}

# 只有分数达到这个值才认为引用真的落到了某条参考文献上：
# 首作者命中 7~10 分，作者集合完整命中 3~4 分，仅作者区文本含同姓 3 分。
# 共同作者（不是首作者）不再给分，避免 `Zhang et al. 2021` 抢走
# `Song et al. 2021` 那篇（Zhang 只是该文第 4 作者）。
_MIN_SCORE = 3


@dataclass
class Citation:
    marker: str
    index: int | None = None
    surname: str | None = None
    year: int | None = None
    start: int = 0
    end: int = 0
    suffix: str = ""
    authors: list[str] = field(default_factory=list)


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


def _ref_suffix(ref: Reference, year: int) -> str:
    if ref.suffix:
        return ref.suffix
    m = re.search(rf"\b{year}([a-z])\b", ref.raw)
    return m.group(1) if m else ""


def _score(cite: Citation, ref: Reference) -> int:
    """这条参考文献有多像正文里的引用；< 0 表示直接排除。"""
    if not ref.year or ref.year != cite.year:
        return -1

    ref_suffix = _ref_suffix(ref, cite.year)
    if cite.suffix and ref_suffix and cite.suffix != ref_suffix:
        return -1
    # 有后缀精确命中给高分；正文没写后缀时优先无后缀/ a 的那篇
    if cite.suffix:
        score = 4 if ref_suffix == cite.suffix else 2
    else:
        score = 1 if ref_suffix in ("", "a") else 0

    names = [_ascii(a) for a in (cite.authors or []) if a]
    ref_names = [_ascii(n) for n in (ref.authors or []) if n]

    if names:
        first = names[0]
        if ref_names and first == ref_names[0]:
            score += 6
        elif ref_names and len(names) >= 2 and set(names) <= set(ref_names):
            # PDF 把 given name 当姓、或正文用了另一作者开头时，作者集合完整命中
            score += 3
        elif (not ref_names or len(ref_names) == 1) and _name_match(
            cite.surname or first, ref.raw
        ):
            # 作者区解析不完整：退回旧的字面匹配，但只在候选极少时才认
            score += 2
        else:
            return -1
    else:
        if not _name_match(cite.surname or "", ref.raw):
            return -1
        score += 2

    return score


def find(text: str) -> list[Citation]:
    cites: list[Citation] = []
    for m in _NUM_RE.finditer(text):
        for n in _expand(m.group(1)):
            cites.append(Citation(marker=m.group(0), index=n, start=m.start(), end=m.end()))

    # 作者-年份：扫描每个年份，往前找最近的作者串（支持任意作者数）
    for m in _YEAR_RE.finditer(text):
        pre = text[max(0, m.start() - 160): m.start()]
        tail = _AUTHOR_TAIL_RE.search(pre)
        if not tail:
            continue
        authors = re.findall(_SURNAME, tail.group(1))
        if not authors:
            continue
        if authors[0].lower() in _STOP_SURNAMES:
            continue
        start = max(0, m.start() - len(tail.group(0)))
        cites.append(
            Citation(
                marker=text[start:m.end()].strip(),
                surname=authors[0],
                year=int(m.group(1)),
                suffix=m.group(2) or "",
                authors=authors,
                start=start,
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


def match_index(
    cite: Citation,
    references: list[Reference],
    used: set[int] | None = None,
) -> int | None:
    """把一个引用标记落到具体参考文献上；落不到就返回 None。

    `used` 是已分配的条目编号：同一条参考文献不再分给第二个引用，
    这样 `Kipf and Welling 2016a/b` 才能各自落到自己的条目上。
    """
    if cite.index is not None:
        return cite.index if any(r.index == cite.index for r in references) else None

    best: int | None = None
    best_score = _MIN_SCORE - 1
    for ref in references:
        if used and ref.index in used:
            continue
        score = _score(cite, ref)
        if score > best_score:
            best = ref.index
            best_score = score
    return best


def marker_key(marker: str) -> str:
    """归一化引用标记：忽略括号、大小写、`&` 与 `and` 的差异。"""
    value = (
        marker.replace("&", "and")
        .replace("(", "")
        .replace(")", "")
        .replace("（", "")
        .replace("）", "")
    )
    return re.sub(r"\s+", " ", value).strip().lower()


def resolve(cites: list[Citation], references: list[Reference]) -> list[dict]:
    """把标记解析成具体文献；解析不到的直接丢掉（宁缺毋滥）。

    同一个标记在正文里出现多次只输出一次；写法变体
    （`Kipf and Welling 2016a` / `Kipf & Welling 2016a`）会复用已解析结果，
    但不同标记不会被强行分到同一条文献上。
    """
    by_index = {r.index: r for r in references}
    out: list[dict] = []
    seen: set[str] = set()
    known: dict[str, int] = {}
    used: set[int] = set()

    for c in cites:
        key = marker_key(c.marker)
        idx = known.get(key)
        if idx is None:
            idx = match_index(c, references, used)
        ref = by_index.get(idx) if idx is not None else None
        if not ref:
            continue
        known[key] = ref.index
        if key in seen:
            continue
        seen.add(key)
        used.add(ref.index)
        out.append({"marker": c.marker, "ref": ref.to_dict()})
    return out