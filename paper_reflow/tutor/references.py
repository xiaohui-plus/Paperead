"""解析论文的参考文献块，为「前置知识」提供候选。

比全局检索靠谱得多：本文引用过的、更早的、名字对得上的，才可能是"前置"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..layout import Block

_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
# 去掉作者 + 年份前缀： "Kipf, T. N., and Welling, M. 2017. Title..."
_PREFIX_RE = re.compile(r"^.{0,120}?\b(?:19|20)\d{2}[a-z]?\.\s*")
_LEAD_AUTHORS_RE = re.compile(r"^[A-Z][^.]{0,160}?\b(19|20)\d{2}\b[^.]*?[.:]\s*")


@dataclass
class Reference:
    index: int
    raw: str
    title: str
    year: int | None

    def to_dict(self) -> dict:
        return {"index": self.index, "title": self.title, "year": self.year, "raw": self.raw}


_NAME_LIKE = re.compile(r"^(?:[A-Z][A-Za-z\-']+|[A-Z]\.)(?:\s*(?:,|and|&)\s*(?:[A-Z][A-Za-z\-']+|[A-Z]\.))*")


def _clean_title(raw: str) -> str:
    t = raw.strip()
    t = re.sub(r"^\[?\d+\]?[.)]?\s*", "", t)      # [12] / 12. 前缀
    t = re.sub(r"^and\s+", "", t)

    # 参考文献常见两段式：作者串. 标题. 期刊/年份
    parts = [p.strip() for p in re.split(r"\.\s+", t) if p.strip()]
    if not parts:
        return t[:200]

    def score(p: str) -> float:
        words = re.findall(r"[A-Za-z]{3,}", p)
        lower = re.findall(r"\b[a-z]{3,}\b", p)
        if not words:
            return -1
        return len(lower) - 0.6 * p.count(",") - (0.4 * len(words) if len(words) < 3 else 0)

    best = max(parts[:4], key=score)
    best = re.sub(r"\s+", " ", best).strip(" .;")
    return best[:200]


_NUM_START_RE = re.compile(r"^\[?(\d{1,3})\]?[.)]?\s+")


def parse(blocks: list[Block]) -> list[Reference]:
    """按「编号开头」分组：一条文献经常被版面切成 2–3 块。

    序号优先用参考文献自己印的编号（`31.` / `[31]`），
    这样正文里的 `[31]` 才能直接对上；没有编号的才用顺序号。
    """
    entries: list[tuple[int | None, list[str]]] = []
    cur_num: int | None = None
    cur_parts: list[str] = []

    for b in blocks:
        if b.kind != "reference":
            continue
        text = (b.text or "").strip()
        if len(text) < 6:
            continue
        m = _NUM_START_RE.match(text)
        if m:
            if cur_parts:
                entries.append((cur_num, cur_parts))
            cur_num = int(m.group(1))
            cur_parts = [text]
        elif cur_parts:
            cur_parts.append(text)
        else:
            cur_parts = [text]

    if cur_parts:
        entries.append((cur_num, cur_parts))

    out: list[Reference] = []
    for seq, (num, parts) in enumerate(entries, 1):
        text = ""
        for part in parts:
            if not text:
                text = part
            elif text.endswith("-"):
                frag = part.split(" ", 1)[0]
                text = (text[:-1] + part) if (len(frag) <= 4 and frag.isalpha() and frag.islower()) else (text + part)
            else:
                text = f"{text} {part}"

        if len(text) < 20:
            continue
        ym = _YEAR_RE.search(text)
        out.append(
            Reference(
                index=num if num is not None else seq,
                raw=text,
                title=_clean_title(text),
                year=int(ym.group(0)) if ym else None,
            )
        )
    return out
