"""解析论文的参考文献块，为「前置知识」「脉络」提供候选。

比全局检索靠谱得多：本文引用过的、更早的、名字对得上的，才可能是"前置"。

PDF 抽出的参考文献经常有三种坏情况，这里逐一处理：
1. 一个文本块里塞了好几条文献（`[2] ... [3] ...`，或作者-年份格式连写）；
2. IEEE 把标题放在引号里，作者串和期刊串都很像标题；
3. 作者-年份格式（AAAI 等）没有编号，标题紧跟年份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..layout import Block

_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
# 去掉作者 + 年份前缀： "Kipf, T. N., and Welling, M. 2017. Title..."
_PREFIX_RE = re.compile(r"^.{0,120}?\b(?:19|20)\d{2}[a-z]?\.\s*")
# 文献编号开头：`[12]` / `12.` / `(12)`
_NUM_START_RE = re.compile(r"^\s*[\[(]?(\d{1,3})[\])]?[.)]?\s+")
# 块内嵌的编号：`...[3] Y. Zhang...`；或 Nature 的 `... (2025). 2. Penadés...`
_EMBEDDED_NUM_RE = re.compile(r"(?=\[\d{1,3}\]\s)")
_EMBEDDED_DOT_RE = re.compile(r"(?<=[.)\]])\s+(?=\d{1,3}\.\s+[A-Z])")
_EMBEDDED_PAREN_RE = re.compile(r"(?<=[.)\]])\s+(?=\(\d{1,3}\)\s+[A-Z])")
# 作者-年份条目连写：句末空白后出现 "Davidson, T. R.; ... 2018."
# 排除紧跟在文献编号后的 "12. Leslie"（那不是新条目，是上一条的编号）
_AAAI_BREAK_RE = re.compile(
    r"(?<![0-9]\.)(?<![0-9][0-9]\.)(?<![0-9][0-9][0-9]\.)"
    r"(?<=[.!?])\s+(?=[A-Z][A-Za-z\-'’]{2,},\s+[A-Z]\.)"
)
# 引号标题（IEEE 最常见）
_QUOTED_RE = re.compile(r"[“\"']([^”\"']{8,240})[”\"']")
# "Author et al. 2021. Title" / "Author, X. 2021. Title"
_YEAR_PREFIX_RE = re.compile(r"^\s*(.{0,180}?)\b((?:19|20)\d{2})[a-z]?\.\s+(.{8,})", re.S)
# 括号年份：`Wang H, ... et al (2023) Title...`
_BRACKET_YEAR_RE = re.compile(r"\((?:19|20)\d{2}[a-z]?\)\s*")


@dataclass
class Reference:
    index: int
    raw: str
    title: str
    year: int | None

    def to_dict(self) -> dict:
        return {"index": self.index, "title": self.title, "year": self.year, "raw": self.raw}


def _pieces(text: str) -> list[str]:
    """把一个 reference block 拆成尽量独立的条目片段。"""
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    for part in _EMBEDDED_NUM_RE.split(text):
        for chunk in _EMBEDDED_PAREN_RE.split(part):
            for p in _EMBEDDED_DOT_RE.split(chunk):
                p = p.strip()
                if not p:
                    continue
                out.extend(x.strip() for x in _AAAI_BREAK_RE.split(p) if x.strip())
    return out


def _looks_author_year(text: str) -> bool:
    """是不是「作者 + 年份 + 标题」开头，而不是上一句的续行。"""
    m = _YEAR_PREFIX_RE.match(text)
    if not m:
        return False
    authors = m.group(1).strip()
    if not authors or len(authors) > 160:
        return False
    if not re.match(r"^(?:[A-Z][A-Za-z\-'’]+|[A-Z]\.)", authors):
        return False
    # 作者串几乎都带姓名缩写（`A.`）或 `et al`；
    # 连续正文（`neural networks ... 2021.`）不会带这些特征。
    if re.search(r"\b[A-Z]\.", authors) or re.search(r"et\s+al", authors, re.I):
        return True
    return bool(re.fullmatch(r"[A-Z][A-Za-z\-'’]+(?:\s+and\s+[A-Z][A-Za-z\-'’]+)?", authors))



def _is_continuation(piece: str, prev: str | None) -> bool:
    """没有编号的碎片是不是上一条文献的续行。"""
    if not prev:
        return False
    if prev.rstrip().endswith(("-", "—")):
        return True
    return not _ENTRY_START_RE.match(piece)


# 一条新文献的开头：`Guan, Y.` / `A. Author` / `12.`
_ENTRY_START_RE = re.compile(r"^(?:[A-Z][A-Za-z\-'’]+,\s*(?:[A-Z]\.|[A-Z][a-z])|[A-Z]\.\s|\d{1,3}\.\s)")


_REF_HEAD_RE = re.compile(r"^[\W_]*\s*(?:references?|bibliography|参考文献)\s*$", re.I)
_NUM_LINE_RE = re.compile(r"^\s*[\[(]?\d{1,3}[\])]?[\t. ]")


def _fallback_reference_blocks(blocks: list[Block]) -> list[str]:
    """部分 PDF 没把参考文献标成 reference，退化成普通段落。

    先找「References / 参考文献」标题；找不到就取文末连续编号的段落。
    """
    for i in range(len(blocks) - 1, -1, -1):
        if _REF_HEAD_RE.match((blocks[i].text or "").strip()):
            tail = [
                (b.text or "").strip()
                for b in blocks[i + 1:]
                if b.kind in ("para", "reference") and len((b.text or "").strip()) >= 20
            ]
            return tail if len(tail) >= 5 else []

    # 没有标题时，参考文献一般在最后一两页；从该区域第一个编号段落开始取
    pages = [b.page for b in blocks if b.page]
    if not pages:
        return []
    last_page = max(pages)
    start = None
    for i, b in enumerate(blocks):
        if b.page >= last_page - 1 and _NUM_LINE_RE.match(b.text or ""):
            start = i
            break
    if start is None:
        return []
    return [
        (b.text or "").strip()
        for b in blocks[start:]
        if b.kind in ("para", "reference") and len((b.text or "").strip()) >= 20
    ]


def _only_reference_section(blocks: list[Block], fallback: list[str]) -> list[str]:
    def in_refs(section: str) -> bool:
        s = (section or "").lower()
        return "reference" in s or "bibliograph" in s

    canonical = [
        (b.text or "").strip()
        for b in blocks
        if b.kind == "reference" and len((b.text or "").strip()) >= 6 and in_refs(b.section)
    ]
    # canonical 依赖 block.section；分页/标题打断会让一部分条目的 section
    # 变成空或别的章节名。只有 canonical 基本覆盖 fallback 时才收窄，
    # 否则用 fallback，避免把 [14]-[32] 这类条目整段丢掉。
    if canonical and (not fallback or len(canonical) >= len(fallback) * 0.9):
        return canonical
    return fallback or canonical


# 块内任意位置的编号：`[12]` / `(12)` / `12.` / `12)`，后面必须跟内容
_NUM_TOKEN_RE = re.compile(
    r"(?:(?<=\s)|^)(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})[.)])\s+(?=\S)"
)


def _split_numbered(text: str, last_num: int | None) -> list[str]:
    """按「递增的文献编号」补切块内条目。

    PyMuPDF 偶尔把连续几条文献并进同一个块，`_pieces` 的固定模式又会
    漏掉其中几条。这里扫描块内所有编号，只有落在 `last_num+1 .. +20`
    的编号才当作新条目，避免把卷号（`PMLR 43.`）、页码、年份
    （`2024.`）误切成文献。
    """
    cuts: list[tuple[int, int]] = []
    expect = last_num
    for m in _NUM_TOKEN_RE.finditer(text):
        num = next((int(g) for g in m.groups() if g), 0)
        if num < 1:
            continue
        ok = 1 <= num <= 500 if expect is None else expect < num <= expect + 20
        if ok:
            cuts.append((m.start(), num))
            expect = num
    if not cuts:
        return [text]
    out: list[str] = []
    prev = 0
    for pos, _ in cuts:
        if pos > prev:
            out.append(text[prev:pos].strip())
        prev = pos
    out.append(text[prev:].strip())
    return [p for p in out if p]


def _split_entries(blocks: list[Block]) -> list[tuple[int | None, str]]:
    """按编号/作者-年份切分，返回 (印刷编号或 None, 合并后的文本)。"""
    ref_blocks = [
        (b.text or "").strip()
        for b in blocks
        if b.kind == "reference" and len((b.text or "").strip()) >= 6
    ]
    if not ref_blocks:
        ref_blocks = _fallback_reference_blocks(blocks)
    else:
        # reference 块可能只覆盖参考文献区的前几页（页码/分页打断分类
        # 状态），剩余条目落到 para。按 References 标题收集的 fallback
        # 更全时改用 fallback，否则 `[22]` 之后的引用全都对不上。
        fallback = _fallback_reference_blocks(blocks)
        if len(fallback) > len(ref_blocks):
            ref_blocks = fallback
    # 双栏排版偶尔会把正文误标成 reference；有明确 References 章节时只取那一段
    ref_blocks = _only_reference_section(blocks, ref_blocks)
    # 有 `[1]` / `1.` 编号时用编号模式；Nature 这类无编号文献按「一块一条」处理
    numbered = any(_NUM_START_RE.match(t) for t in ref_blocks)

    entries: list[tuple[int | None, list[str]]] = []
    cur_num: int | None = None
    cur_parts: list[str] = []

    def flush() -> None:
        nonlocal cur_num, cur_parts
        if cur_parts:
            entries.append((cur_num, cur_parts))
        cur_num, cur_parts = None, []

    for text in ref_blocks:
        # 编号校验只在「块内」生效：双栏/分页会让参考文献块的顺序错乱
        # （例如 [33..43] 块排在 [14..32] 前面），用全局 last_num 会把
        # 后出现的 [14..32] 全当成编号回退丢掉。
        block_last: int | None = None
        if numbered:
            for raw_piece in _pieces(text):
                for piece in _split_numbered(raw_piece, block_last):
                    m = _NUM_START_RE.match(piece)
                    if m:
                        num = int(m.group(1))
                        # 编号 <= 0、超过 500、块内回退或突跳（>20）说明不是
                        # 文献编号：页码、表格数据行、卷号（`PMLR 43.`）。
                        # 跳过而不是并入上一条，否则会污染内容并制造 717/0 假条目。
                        if num < 1 or num > 500:
                            continue
                        if block_last is not None and (
                            num <= block_last or num > block_last + 20
                        ):
                            continue
                        block_last = num
                        flush()
                        cur_num = num
                        cur_parts = [piece]
                    elif _looks_author_year(piece) and cur_parts and not cur_parts[-1].rstrip().endswith(("-", "—")):
                        flush()
                        cur_parts = [piece]
                    else:
                        cur_parts.append(piece)
        else:
            # 无编号文献通常是「一个文本块一条」；只有明确是续行时才并入上一条
            pieces = _pieces(text)
            for i, piece in enumerate(pieces):
                prev = cur_parts[-1] if cur_parts else None
                if prev and _is_continuation(piece, prev):
                    cur_parts.append(piece)
                else:
                    flush()
                    cur_parts = [piece]
    flush()

    out: list[tuple[int | None, str]] = []
    for num, parts in entries:
        text = ""
        for part in parts:
            if not text:
                text = part
            elif text.endswith("-"):
                frag = part.split(" ", 1)[0]
                # 行尾连字符断词：`net-` + `works` → `networks`
                if len(frag) <= 4 and frag.isalpha() and frag.islower():
                    text = text[:-1] + part
                else:
                    text = text + part
            else:
                text = f"{text} {part}"
        out.append((num, re.sub(r"\s+", " ", text).strip()))
    return out


_NAME_LIKE = re.compile(r"^(?:[A-Z][A-Za-z\-']+|[A-Z]\.)(?:\s*(?:,|and|&)\s*(?:[A-Z][A-Za-z\-']+|[A-Z]\.))*")


def _norm_title(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    # 版面把断词连字符后补了空格：`Con- volutional` → `Convolutional`
    t = re.sub(r"(?<=[a-z])-\s+(?=[a-z])", "", t)
    t = t.strip(" .;,:，。；：")
    t = re.sub(r"^(?:and|et\s+al\.?)\s+", "", t, flags=re.I)
    # 去掉标题后面可能粘上的卷期信息
    t = re.sub(r"[,;]?\s*(?:vol\.?|no\.?|pp?\.?)\s*\d.*$", "", t, flags=re.I)
    return t.strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]{2,}", text))


def _good_title(text: str) -> bool:
    if not text or not (5 <= len(text) <= 240):
        return False
    if _word_count(text) < 2:
        return False
    if text.isupper():
        return False
    if re.search(r"https?://|@|\bdoi\b", text, re.I):
        return False
    return True


def _first_sentence(text: str) -> str:
    # 标题内部很少出现 ". "，遇到就当作标题结束
    return re.split(r"(?<=[a-z0-9])\.\s", text, maxsplit=1)[0]


def _segment_score(p: str) -> float:
    words = re.findall(r"[A-Za-z]{3,}", p)
    if not words:
        return -1e9
    lower = re.findall(r"\b[a-z]{3,}\b", p)
    initials = len(re.findall(r"\b[A-Z]\.", p))
    semis = p.count(";")
    commas = p.count(",")
    caps = sum(1 for c in p if c.isupper()) / max(len(p), 1)
    venue = len(
        re.findall(
            r"\b(?:vol|no|pp|proc|proceedings|conference|journal|transactions|arxiv|springer|press)\b",
            p,
            re.I,
        )
    )
    return (
        len(lower) * 0.9
        + len(words) * 0.2
        - initials * 2.2
        - semis * 1.4
        - commas * 0.35
        - caps * 4.0
        - venue * 2.5
    )


def _clean_title(raw: str) -> str:
    t = re.sub(r"^\[?\d+\]?[.)]?\s*", "", raw.strip())
    t = re.sub(r"^and\s+", "", t)

    # 1) IEEE / Science 风格：标题在引号里，最可靠
    q = _QUOTED_RE.search(t)
    if q:
        cand = _norm_title(q.group(1))
        if _good_title(cand):
            return cand[:240]

    # 2) 作者-年份风格：`Author, X. 2021. Title.`
    m = _YEAR_PREFIX_RE.match(t)
    if m:
        cand = _norm_title(_first_sentence(m.group(3)))
        if _good_title(cand):
            return cand[:240]

    # 2b) 作者-年份用括号：`Wang H, ... et al (2023) Title...`
    m2 = _BRACKET_YEAR_RE.search(t)
    if m2:
        cand = _norm_title(_first_sentence(t[m2.end():]))
        if _good_title(cand):
            return cand[:240]

    # 3) 兜底：按句点切段，挑最像标题的一段
    parts = [p.strip() for p in re.split(r"\.\s+", t) if p.strip()]
    best = ""
    best_score = -1e9
    for p in parts[:6]:
        s = _segment_score(p)
        if s > best_score:
            best_score = s
            best = p
    best = _norm_title(best) or _norm_title(t)
    return best[:240]


def _extract_year(raw: str) -> int | None:
    m = _YEAR_PREFIX_RE.match(raw)
    if m:
        return int(m.group(2))
    q = _QUOTED_RE.search(raw)
    if q:
        ym = _YEAR_RE.search(raw[q.end():])
        if ym:
            return int(ym.group(0))
    # Nature 风格：出版年写在括号里 `(2025).`，比句尾残留的其他年份可靠
    paren = re.findall(r"\((19[5-9]\d|20[0-4]\d)\)", raw)
    if paren:
        return int(paren[-1])
    years = _YEAR_RE.findall(raw)
    # 没有明确前缀时，出版年通常出现在末尾
    return int(years[-1]) if years else None


def parse(blocks: list[Block]) -> list[Reference]:
    """解析参考文献，返回带标题/年份的条目。

    序号优先用参考文献自己印的编号（`31.` / `[31]`），
    这样正文里的 `[31]` 才能直接对上；没有编号的用顺序号。
    """
    out: list[Reference] = []
    used: set[int] = set()
    for seq, (num, text) in enumerate(_split_entries(blocks), 1):
        if len(text) < 20:
            continue
        title = _clean_title(text)
        # 标题抽不出来也不能丢条：正文的 [n] 要靠它对齐到具体文献。
        # 用原文前缀降级保留，比整条消失好（出处仍可用 raw 文本）。
        if not _good_title(title):
            title = _norm_title(text[:160]) or text[:160]
        if num is None:
            # 无编号条目用顺序号；避开正文里已出现的印刷编号
            index = seq
            while index in used:
                index += 1
        else:
            index = num
            if index in used:
                continue
        used.add(index)
        out.append(
            Reference(
                index=index,
                raw=text,
                title=title,
                year=_extract_year(text),
            )
        )
    return out
