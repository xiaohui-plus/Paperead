"""从正文里找出引用标记，并映射到参考文献条目。

支持两种主流写法：
- 数字：`[31]` / `[12,13]` / `[3-5]`
- 作者-年份：`(Kipf and Welling 2016a)` / `(Zhang et al. 2018)`
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .references import Reference

_NUM_RE = re.compile(r"\[(\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*)\]")
_AUTHOR_YEAR_RE = re.compile(
    r"\(([A-Z][A-Za-z\-']+)(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z\-']+))?"
    r"\s*,?\s*((?:19|20)\d{2})[a-z]?\)"
)


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


def find(text: str) -> list[Citation]:
    cites: list[Citation] = []
    for m in _NUM_RE.finditer(text):
        for n in _expand(m.group(1)):
            cites.append(Citation(marker=m.group(0), index=n, start=m.start(), end=m.end()))

    for m in _AUTHOR_YEAR_RE.finditer(text):
        cites.append(
            Citation(
                marker=m.group(0),
                surname=m.group(1),
                year=int(m.group(2)),
                start=m.start(),
                end=m.end(),
            )
        )
    return cites


def resolve(cites: list[Citation], references: list[Reference]) -> list[dict]:
    """把标记解析成具体文献；解析不到的直接丢掉（宁缺毋滥）。"""
    by_index = {r.index: r for r in references}
    out: list[dict] = []
    seen: set[int] = set()

    for c in cites:
        ref = None
        if c.index is not None:
            ref = by_index.get(c.index)
        elif c.surname and c.year:
            for r in references:
                raw = r.raw
                if c.surname.lower() in raw.lower() and str(c.year) in raw:
                    ref = r
                    break
        if not ref or ref.index in seen:
            continue
        seen.add(ref.index)
        out.append({"marker": c.marker, "ref": ref.to_dict()})
    return out
