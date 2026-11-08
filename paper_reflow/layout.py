"""论文版面重排：把 PDF 拆成按阅读顺序排列的结构化块（段落/标题/公式/图/表）。

设计要点
--------
1. **以「行」为单位处理，而不是 PyMuPDF 的 block**。学术 PDF 里一段标题与正文
   常被合并进同一个 block（例如 `Introduction Link prediction is the task ...`），
   按 block 处理会把标题吞进正文。行级别才能准确切分。
2. **双栏阅读顺序**：先检测栏间空白（gutter），把行分为 左栏 / 右栏 / 跨栏 三类；
   跨栏元素（标题、通栏图、表格）作为「分段锚点」，每个锚点区间内按 左栏→右栏 排序。
   检测不到可靠 gutter 时退回 PDF 原生流顺序（LaTeX 产物的原生顺序通常就是阅读顺序）。
3. **不还原版式**，只重建阅读顺序 + 结构化分块；图片/表格按区域裁剪成 PNG，
   以便前端「原文对照」与公式展示。

坐标系与 PyMuPDF 一致：左上原点，单位 pt。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import pymupdf

# ── 文本清洗 ───────────────────────────────────────────────────────────

# LaTeX 断词：行尾连字符
_HYPHEN_END = re.compile(r"([A-Za-z])-\s*$")
# 章节标题（行首，允许前面有编号）
_SECTION_RE = re.compile(
    r"^(?P<head>(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(?:Abstract|Introduction|Related Works?|Background|Preliminaries?|"
    r"Materials? and Methods|Methods?|Approach|Model|Experiments?|"
    r"Results?|Discussion|Conclusions?|References|Bibliography|"
    r"Acknowledg(?:e)?ments?|Appendix(?:\s+[A-Z])?)\b)\s*",
    re.IGNORECASE,
)
# 编号小节标题：2.1 Foo Bar
_NUM_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z]")
# 图注 / 表注
_CAPTION_RE = re.compile(r"^(?:Figure|Fig\.|FIGURE|Table|TABLE|Tab\.)\s*\d+", re.IGNORECASE)
# 数学字体（Computer Modern 数学族）
_MATH_FONTS = ("CMMI", "CMEX", "CMSY", "MSBM", "MTMI", "MTEX", "EUSM", "EUFM", "Symbol")

# 页眉页脚判定：页面上下各 8% 区域
_HEADER_RATIO = 0.075
_FOOTER_RATIO = 0.94


@dataclass
class Line:
    """一行文本及其排版属性。"""

    text: str
    bbox: tuple[float, float, float, float]
    page: int
    size: float = 0.0          # 主字号（按字符数加权）
    bold: bool = False
    math: bool = False         # 是否数学字体行
    col: int = 0               # 0/1 栏号；-1 表示跨栏
    is_margin: bool = False    # 页眉页脚
    math_chars: int = 0        # 数学字体字符数（判断「是否公式行」）
    total_chars: int = 0
    baseline: float = 0.0      # 文字基线 y（判断「是否同一行」远比 bbox 中心可靠）

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2


@dataclass
class Block:
    """结构化内容块。"""

    id: str
    order: int
    page: int
    kind: str                       # title/author/affiliation/abstract/heading/para/
                                    # equation/caption/figure/table/reference/margin
    text: str
    bbox: tuple[float, float, float, float]
    level: int = 0                  # heading 层级：1/2/3
    size: float = 0.0               # 主字号（标题识别用）
    section: str = ""               # 所属章节标题（翻译上下文用）
    image_path: str | None = None   # figure/table/equation 的裁剪图
    caption: str = ""               # figure/table 的图注
    table_html: str = ""            # 表格结构化结果（find_tables 成功时）
    latex: str = ""                 # 公式的 LaTeX（formula.py 识别后写入）
    translatable: bool = True
    translated: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order": self.order,
            "page": self.page,
            "kind": self.kind,
            "level": self.level,
            "section": self.section,
            "text": self.text,
            "caption": self.caption,
            "image_path": self.image_path,
            "table_html": self.table_html,
            "latex": self.latex,
            "translatable": self.translatable,
            "translated": self.translated,
            "bbox": [round(v, 1) for v in self.bbox],
        }


@dataclass
class ParsedPaper:
    page_count: int
    blocks: list[Block] = field(default_factory=list)
    body_size: float = 10.0
    columns: int = 1


# ── 基础提取 ───────────────────────────────────────────────────────────

def _span_style(span: dict) -> tuple[float, bool, bool]:
    """返回 (字号, 是否粗体, 是否数学字体)。"""
    size = float(span.get("size") or 0)
    font = (span.get("font") or "")
    flags = int(span.get("flags") or 0)
    bold = bool(flags & 16) or bool(
        re.search(r"Bold|Medi|Heavy|Black|Semibold|-Bd", font, re.IGNORECASE)
    )
    math = any(font.startswith(m) for m in _MATH_FONTS)
    return size, bold, math


def _join_spans(spans: list[dict]) -> str:
    """按 span 的 bbox 间距拼接文本。

    部分 PDF（如 AAAI 的 LaTeX 产物）把单词逐个拆成 span 且不带空格字符，
    直接 join 会得到 `centralnodethroughtheMessagePassing`。这里用字距判定补空格。
    """
    parts: list[str] = []
    prev_x1: float | None = None
    for s in spans:
        t = s.get("text") or ""
        if not t:
            continue
        x0, x1 = s["bbox"][0], s["bbox"][2]
        size = float(s.get("size") or 10.0)
        if parts and prev_x1 is not None:
            gap = x0 - prev_x1
            last = parts[-1]
            if (
                gap > max(0.18 * size, 1.0)
                and not last.endswith((" ", "-", "/", "\u00ad"))
                and not t.startswith(" ")
            ):
                parts.append(" ")
        parts.append(t)
        prev_x1 = x1
    return "".join(parts)


def _line_from_dict(raw: dict, page_no: int) -> Line | None:
    # 竖排/旋转文字（期刊页边 "Downloaded from ..." 水印、旋转图注）不参与正文
    direction = raw.get("dir") or (1, 0)
    if abs(float(direction[1])) > 0.15:
        return None
    spans = [s for s in raw.get("spans") or [] if (s.get("text") or "").strip()]
    if not spans:
        return None
    text = _join_spans(spans)
    if not text.strip():
        return None
    # 按字符数加权的主字号 / 粗体 / 数学字体
    weights = Counter()
    bold_chars = 0
    math_chars = 0
    main_span = spans[0]
    for s in spans:
        n = len(s["text"])
        size, bold, math = _span_style(s)
        weights[round(size, 1)] += n
        if bold:
            bold_chars += n
        if math:
            math_chars += n
        if n > len(main_span["text"]):
            main_span = s
    total = sum(weights.values()) or 1
    size = weights.most_common(1)[0][0] if weights else 0.0
    origin = main_span.get("origin") or (0.0, raw["bbox"][3])
    return Line(
        text=text,
        bbox=tuple(raw["bbox"]),
        page=page_no,
        size=size,
        bold=bold_chars / total > 0.5,
        math=math_chars / total > 0.6,
        math_chars=math_chars,
        total_chars=total,
        baseline=float(origin[1]),
    )


def _collect_lines(doc: pymupdf.Document) -> list[Line]:
    lines: list[Line] = []
    for pno in range(len(doc)):
        page = doc[pno]
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for raw in b.get("lines") or []:
                ln = _line_from_dict(raw, pno + 1)
                if ln is not None:
                    lines.append(ln)
    return lines


def _mark_margins(lines: list[Line], doc: pymupdf.Document) -> None:
    """标记页眉/页脚：几何位置 + 跨页重复文本双重判定。"""
    heights = {p: doc[p].rect.height for p in range(len(doc))}
    for ln in lines:
        h = heights.get(ln.page - 1, 792)
        if ln.y1 < h * _HEADER_RATIO or ln.y0 > h * _FOOTER_RATIO:
            ln.is_margin = True
    # 同一文本在多页重复出现（≥ 3 页且 ≥ 30% 篇幅）→ 页眉页脚
    if len(doc) >= 3:
        norm = Counter()
        for ln in lines:
            if ln.is_margin:
                continue
            key = re.sub(r"\d+", "#", ln.text.strip().lower())
            if 3 <= len(key) <= 120:
                norm[key] += 1
        threshold = max(3, int(len(doc) * 0.3))
        repeats = {k for k, c in norm.items() if c >= threshold}
        for ln in lines:
            key = re.sub(r"\d+", "#", ln.text.strip().lower())
            if key in repeats and ln.page > 1:
                ln.is_margin = True


# ── 栏检测 ─────────────────────────────────────────────────────────────

def _text_area(lines: list[Line]) -> tuple[float, float, float, float]:
    xs0 = [ln.x0 for ln in lines]
    xs1 = [ln.x1 for ln in lines]
    ys0 = [ln.y0 for ln in lines]
    ys1 = [ln.y1 for ln in lines]
    return min(xs0), min(ys0), max(xs1), max(ys1)


def _detect_gutter(lines: list[Line]) -> float | None:
    """检测双栏之间的空白带，返回 gutter 中心 x；单栏返回 None。

    做法：只统计「栏宽级别」的行（宽度 < 文本区 62%），把它们投影到 x 轴，
    在页面中部寻找一段足够宽、覆盖率接近 0 的空白带。
    """
    body = [ln for ln in lines if not ln.is_margin and ln.text.strip()]
    if len(body) < 8:
        return None
    x0, _, x1, _ = _text_area(body)
    width = x1 - x0
    if width <= 0:
        return None

    col_lines = [ln for ln in body if ln.width < width * 0.62]
    if len(col_lines) < 0.45 * len(body):
        return None  # 大部分是通栏宽行 → 单栏

    bins = int(width) + 1
    cover = [0] * bins
    for ln in col_lines:
        a = max(0, min(bins - 1, int(ln.x0 - x0)))
        b = max(0, min(bins - 1, int(ln.x1 - x0)))
        for i in range(a, b + 1):
            cover[i] += 1

    lo, hi = int(bins * 0.32), int(bins * 0.68)
    # 「空白」阈值按峰值覆盖率取比例：gutter 内可能有少量跨栏长行，
    # 不能要求严格为 0（AAAI 这类 PDF 的 gutter 覆盖率约 1%，正文处 350+）
    peak = max(cover) if cover else 0
    tol = max(2, int(peak * 0.05))
    best = (0, None, None)  # (宽度, start, end)
    i = lo
    while i < hi:
        if cover[i] <= tol:
            j = i
            while j + 1 < hi and cover[j + 1] <= tol:
                j += 1
            if j - i > best[0]:
                best = (j - i, i, j)
            i = j + 1
        else:
            i += 1

    gap_w, s, e = best
    if gap_w < 8 or s is None:
        return None
    # 两侧都必须有足够多的文字，否则不是真 gutter
    left = sum(1 for ln in col_lines if ln.x1 <= x0 + s)
    right = sum(1 for ln in col_lines if ln.x0 >= x0 + e)
    if left < len(col_lines) * 0.15 or right < len(col_lines) * 0.15:
        return None
    return x0 + (s + e) / 2


def _assign_columns(lines: list[Line], gutter: float | None) -> None:
    for ln in lines:
        if ln.is_margin or gutter is None:
            ln.col = 0
        elif ln.x0 < gutter - 3 and ln.x1 > gutter + 3:
            ln.col = -1          # 跨栏
        elif ln.cx < gutter:
            ln.col = 0
        else:
            ln.col = 1


def _merge_visual_rows(lines: list[Line]) -> list[Line]:
    """把同一基线上的文本碎片合并成一条「视觉行」。

    LaTeX 会把行内公式（如 `Ã := D^{-1/2}`）画成独立的 glyph run，PyMuPDF 因此
    把它们识别成「同一基线上的另一条 line」。不合并的话，一句
    `Where Ã := ... denotes the normalized adjacency matrix.` 会被切成三块，
    翻译出来就是断句。合并后仍是 Line 对象，下游逻辑无需改动。
    """
    groups: dict[tuple[int, int], list[Line]] = {}
    for ln in lines:
        if ln.is_margin:
            continue
        groups.setdefault((ln.page, ln.col), []).append(ln)

    rows: list[Line] = []
    for (page, col), group in groups.items():
        group.sort(key=lambda l: l.y0)
        # 按「文字基线」聚类：上标/下标、高公式都不会把下一行误并进来
        clusters: list[list[Line]] = []
        for ln in group:
            for c in clusters:
                ref = c[0]
                # 容差取两行中较大的字号：上标/下标相对父字号偏移约 0.33em，
                # 用较小字号算会让 10pt 正文里的 6pt 上标（偏移 3pt）刚好合不进来
                tol = 0.45 * max(ln.size or 10.0, ref.size or 10.0)
                if abs(ln.baseline - ref.baseline) <= tol:
                    c.append(ln)
                    break
            else:
                clusters.append([ln])

        for c in clusters:
            c.sort(key=lambda l: l.x0)      # 同一行内按 x 从左到右拼
            parts: list[str] = []
            prev_x1: float | None = None
            for m in c:
                if parts and prev_x1 is not None and m.x0 - prev_x1 > 0.18 * max(m.size, 1.0):
                    parts.append(" ")
                parts.append(m.text)
                prev_x1 = m.x1 if prev_x1 is None else max(prev_x1, m.x1)

            total = sum(m.total_chars for m in c) or 1
            math_chars = sum(m.math_chars for m in c)
            main = max(c, key=lambda m: m.total_chars)
            rows.append(
                Line(
                    text="".join(parts).strip(),
                    bbox=(
                        min(m.bbox[0] for m in c), min(m.bbox[1] for m in c),
                        max(m.bbox[2] for m in c), max(m.bbox[3] for m in c),
                    ),
                    page=page,
                    col=col,
                    size=main.size,
                    bold=sum(1 for m in c if m.bold) * 2 > len(c),
                    math=math_chars / total > 0.6,
                    math_chars=math_chars,
                    total_chars=total,
                    baseline=main.baseline,
                )
            )
    return [r for r in rows if r.text.strip()]


def _reading_order(lines: list[Line], gutter: float | None) -> list[Line]:
    """按阅读顺序返回行序列。"""
    body = [ln for ln in lines if not ln.is_margin]
    if gutter is None:
        # 原生流顺序（LaTeX 通常已是正确顺序）
        return sorted(body, key=lambda ln: (ln.page, ln.y0, ln.x0))

    ordered: list[Line] = []
    pages = sorted({ln.page for ln in body})
    for page in pages:
        plines = [ln for ln in body if ln.page == page]
        spanning = sorted([ln for ln in plines if ln.col == -1], key=lambda l: l.y0)
        col_lines = sorted(
            [ln for ln in plines if ln.col in (0, 1)], key=lambda l: (l.y0, l.x0)
        )

        # 以跨栏元素为锚点切分 y 区间：锚点之上按 左栏→右栏 输出，再输出锚点本身
        prev_y = float("-inf")
        for sp in spanning:
            region = [ln for ln in col_lines if prev_y <= ln.y0 < sp.y0]
            ordered.extend(_col_major(region))
            ordered.append(sp)
            prev_y = sp.y0
        tail = [ln for ln in col_lines if ln.y0 >= prev_y]
        ordered.extend(_col_major(tail))
    return ordered


def _col_major(lines: list[Line]) -> list[Line]:
    """同区内：先左栏（按 y），再右栏（按 y）。"""
    left = sorted([ln for ln in lines if ln.col == 0], key=lambda l: (l.y0, l.x0))
    right = sorted([ln for ln in lines if ln.col == 1], key=lambda l: (l.y0, l.x0))
    return left + right


# ── 行 → 段落重组 ──────────────────────────────────────────────────────

def _guess_body_size(lines: list[Line]) -> float:
    weights = Counter()
    for ln in lines:
        if ln.is_margin:
            continue
        n = len(ln.text)
        if 20 <= n <= 400:  # 正文行长
            weights[ln.size] += n
    return weights.most_common(1)[0][0] if weights else 10.0


def _join_text(prev: str, nxt: str) -> str:
    """合并两行：处理 LaTeX 断词与空格。"""
    prev = prev.rstrip()
    nxt = nxt.lstrip()
    if not prev:
        return nxt
    if not nxt:
        return prev
    if _HYPHEN_END.search(prev) and nxt[:1].islower():
        return prev[:-1] + nxt          # inter-\nnational → international
    if prev.endswith("-") and nxt[:1].isupper():
        return prev + nxt               # 保留连字符（如 3D-CNN）
    return prev + " " + nxt


def _is_heading(ln: Line, body_size: float) -> int:
    """判断行是否为标题，返回层级（0 表示不是标题）。"""
    text = ln.text.strip()
    if not text or len(text) > 120:
        return 0
    if ln.size >= body_size + 3.5:
        return 1
    if ln.size >= body_size + 1.2:
        return 2
    m = _SECTION_RE.match(text)
    # 只认首字母大写的章节名：「methods take the random walk ...」是正文，不是标题
    if m and m.group("head")[:1].isupper() and len(m.group("head")) <= 40 and len(text) <= 60:
        return 1 if ln.size >= body_size else 2
    if ln.bold and len(text) <= 70 and not text.endswith((".", "?", ";", ",")):
        return 3
    if _NUM_HEADING_RE.match(text) and ln.bold:
        return 3
    return 0


def _split_line_heading(ln: Line, body_size: float) -> tuple[str, str] | None:
    """标题与正文被合并进同一行时（如 "Introduction Link prediction is ..."）拆开。"""
    text = ln.text.strip()
    m = _SECTION_RE.match(text)
    if not m:
        return None
    head = m.group("head").strip()
    rest = text[m.end():].strip()
    if len(head) > 60 or len(rest) < 30:
        return None
    if not head[0].isupper():
        return None
    return head, rest


_REL_RE = re.compile(r"[=≈≤≥≃∼<>\u2211\u222b\u220f]")
_WORD_RE = re.compile(r"[A-Za-z]{3,}")
# 私用区字符：LaTeX 把大括号/根号等大型定界符映射到 U+E000–U+F8FF（CMEX 字体），
# 浏览器渲染出来就是豆腐块。含这类字符的行一定是公式，不能当正文翻译。
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")
_STOP_RE = re.compile(
    r"\b(?:the|of|and|is|are|was|were|in|on|to|for|with|by|that|this|these|those|"
    r"where|which|when|we|our|it|its|as|at|from|be|been|has|have|not|but|than|"
    r"denotes|denote|represent|representing|respectively|such|can|may|will)\b",
    re.IGNORECASE,
)
# 以连接词开头的行是行文，不是公式：`where q_i = h_iW^Q, k_j = h_jW^K.` 这类行
# 数学字体占比很高（>60%），只靠字体比例会被误判成公式并整段吞掉。
_PROSE_LEAD_RE = re.compile(
    r"^(?:where|when|if|then|here|hence|thus|therefore|with|for|and|or|but|so|"
    r"such\s+that|in\s+which|that\s+is)\b",
    re.IGNORECASE,
)


def _looks_equation(text: str, is_math_font: bool) -> bool:
    """公式行：数学字体占比高，或几乎没有自然语言词。

    注意不能用「词少就算公式」这种宽松规则——`Where Ã := … denotes the normalized
    adjacency matrix.` 这类含行内公式的正文句子只有 4 个英文词，会被误判成公式，
    进而被并进上面的显示公式里、整段不翻译。
    """
    if is_math_font:
        # 数学字体占主导也可能是行文（"where q_i = ..."），先排除
        if _PROSE_LEAD_RE.match(text.strip()):
            return False
        return True
    t = text.strip()
    if not t or len(t) > 160:
        return False
    # 私用区字形（大括号等）几乎只出现在公式里；限制英文词数，避免把带项目符号的正文误判
    if _PUA_RE.search(t) and len(_WORD_RE.findall(t)) <= 6:
        return True
    if not _REL_RE.search(t):
        return False
    if len(_WORD_RE.findall(t)) > 2:
        return False
    return not _STOP_RE.search(t)


def _build_blocks(ordered: list[Line], body_size: float) -> list[Block]:
    blocks: list[Block] = []
    cur: dict | None = None
    line_h = median([ln.height for ln in ordered]) if ordered else 12.0

    # 每栏的左右边界，用于判断「行是否写满」与「是否首行缩进」
    edges: dict[int, tuple[float, float]] = {}
    for c in (0, 1):
        ls = [ln for ln in ordered if ln.col == c]
        if ls:
            edges[c] = (min(ln.x0 for ln in ls), max(ln.x1 for ln in ls))

    def flush():
        nonlocal cur
        if cur and cur["text"].strip():
            blocks.append(
                Block(
                    id="",
                    order=len(blocks),
                    page=cur["page"],
                    kind=cur["kind"],
                    text=cur["text"].strip(),
                    bbox=cur["bbox"],
                    level=cur["level"],
                    size=cur.get("size", 0.0),
                )
            )
        cur = None

    def start(ln: Line, text: str | None = None) -> dict:
        return {
            "text": text if text is not None else ln.text,
            "page": ln.page, "kind": "para", "bbox": ln.bbox, "level": 0,
            "size": ln.size, "bold": ln.bold, "col": ln.col,
            "last_y1": ln.y1, "last_x0": ln.x0, "last_x1": ln.x1,
        }

    eq: dict | None = None

    def flush_eq():
        nonlocal eq
        if eq and eq["text"].strip():
            blocks.append(
                Block(
                    id="", order=len(blocks), page=eq["page"], kind="equation",
                    text=eq["text"].strip(), bbox=eq["bbox"], translatable=False,
                    size=eq.get("size", 0.0),
                )
            )
        eq = None

    def gap_to(state: dict, ln: Line) -> float:
        if state["page"] != ln.page:
            return 1e9
        return ln.y0 - state["last_y1"]

    for ln in ordered:
        # ── 公式：连续数学行合成一个公式块，不参与正文合并、不翻译 ──
        math_ratio = ln.math_chars / max(ln.total_chars, 1)
        # 紧贴在公式下方/上方、又短的片段，一律算作同一个公式的一部分：
        # 「log(dz),」「z∈N(u)∩N(v)」这类分母/下标单独看既不是公式也不是正文，
        # 靠数学字体比例判断会漏，只能靠几何关系收编。
        absorb = (
            eq is not None
            and gap_to(eq, ln) <= 4.0
            and len(ln.text) <= 80
            # 用停用词判断行文，而不是英文词数：公式行里的 `PkeySPD`、`Jaccard`
            # 也会被当成「长单词」，按词数卡会把它们误判成正文
            and not _STOP_RE.search(ln.text)
            and not _PROSE_LEAD_RE.match(ln.text.strip())
        )
        if (_looks_equation(ln.text, ln.math) or absorb) and not _is_heading(ln, body_size):
            flush()
            # 同一公式的各行在纵向上互相重叠或几乎相接；两个相邻但不挨着的公式
            # （中间有 10pt 以上的空白）必须分开，否则裁图会把后一个公式拦腰截断
            if eq is not None and gap_to(eq, ln) <= 4.0:
                eq["text"] = eq["text"] + " " + ln.text.strip()
                eq["bbox"] = (
                    min(eq["bbox"][0], ln.bbox[0]), min(eq["bbox"][1], ln.bbox[1]),
                    max(eq["bbox"][2], ln.bbox[2]), max(eq["bbox"][3], ln.bbox[3]),
                )
                eq["last_y1"] = ln.y1
            else:
                flush_eq()
                eq = {
                    "text": ln.text.strip(), "page": ln.page, "bbox": ln.bbox,
                    "last_y1": ln.y1, "size": ln.size,
                }
            continue
        flush_eq()

        head_level = _is_heading(ln, body_size)
        split = _split_line_heading(ln, body_size) if head_level else None

        if split:
            head, rest = split
            flush()
            blocks.append(
                Block(
                    id="", order=len(blocks), page=ln.page, kind="heading",
                    text=head, bbox=ln.bbox, level=1, size=ln.size,
                )
            )
            cur = start(ln, rest)
            continue

        if head_level:
            flush()
            blocks.append(
                Block(
                    id="", order=len(blocks), page=ln.page, kind="heading",
                    text=ln.text.strip(), bbox=ln.bbox, level=head_level, size=ln.size,
                )
            )
            continue

        if cur is None:
            cur = start(ln)
            continue

        left, right = edges.get(ln.col, (ln.x0, ln.x1))
        col_w = max(right - left, 1.0)
        prev_short = cur["last_x1"] < right - 0.08 * col_w      # 上一行没写满 → 段落结束
        # 首行缩进：既要相对栏左边界缩进，又要相对上一行缩进（否则整段每行都会被误判）
        indented = (
            ln.x0 - left > 0.8 * ln.size
            and ln.x0 - cur["last_x0"] > 0.5 * ln.size
        )
        gap = ln.y0 - cur["last_y1"]

        new_para = (
            ln.col != cur["col"]
            or gap > max(line_h * 1.55, 6)
            or abs(ln.size - cur["size"]) > 1.0
            or ln.bold != cur["bold"]
            or prev_short
            or indented
        )
        if new_para:
            flush()
            cur = start(ln)
        else:
            cur["text"] = _join_text(cur["text"], ln.text)
            cur["bbox"] = (
                min(cur["bbox"][0], ln.bbox[0]), min(cur["bbox"][1], ln.bbox[1]),
                max(cur["bbox"][2], ln.bbox[2]), max(cur["bbox"][3], ln.bbox[3]),
            )
            cur["last_y1"] = ln.y1
            cur["last_x0"] = ln.x0
            cur["last_x1"] = ln.x1
    flush_eq()
    flush()
    return blocks


# ── 块分类与清理 ───────────────────────────────────────────────────────

_AFFIL_RE = re.compile(
    r"Department|University|Universit|Institute|Laborator|College|Academy|"
    r"School of|Inc\.|Corp|Ltd|Research Center|@",
    re.IGNORECASE,
)
# 单位列表常见形态：上标编号粘在机构名前，如 "adComprehensive Cancer Center"
_AFFIL_MARKER_RE = re.compile(r"^[a-z]{1,3}[A-Z][a-z]")
_KEYWORDS_RE = re.compile(r"^(?:Keywords?|KEY\s*WORDS|Index Terms)\s*[:：]", re.IGNORECASE)
_NAME_RE = re.compile(r"[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z]+")
_FOOTNOTE_RE = re.compile(
    r"copyright\s*©|all rights reserved|contributed equally|"
    r"permission to make digital|^proceedings of the|^arxiv:\s*\d|"
    r"under review|^doi:",
    re.IGNORECASE,
)


def _pick_title_index(blocks: list[Block], page_height: float) -> int | None:
    """选出标题块：首页上部、字号最大、排除 DOI/URL/页眉行。

    不能简单取第一个块——很多期刊首页首行是 DOI、栏目名或下载水印。
    """
    best: tuple[tuple[float, float], int] | None = None
    for i, b in enumerate(blocks[:16]):
        if b.page != 1:
            continue
        t = b.text.strip()
        if len(t) < 12 or len(t) > 300:
            continue
        if re.match(r"^(https?://|doi:|downloaded\s+from|received:|accepted:)", t, re.IGNORECASE):
            continue
        if b.bbox[1] > page_height * 0.45:
            continue
        if b.kind == "equation":
            continue
        key = (round(b.size, 1), -b.bbox[1])
        if best is None or key > best[0]:
            best = (key, i)
    return best[1] if best else None


def _merge_title_lines(blocks: list[Block], idx: int | None) -> None:
    """标题常被排版成多行大字号文本块，按字号把后续块并回标题。"""
    if idx is None or idx >= len(blocks):
        return
    t = blocks[idx]
    j = idx + 1
    while j < len(blocks):
        n = blocks[j]
        if n.page != t.page or abs(n.size - t.size) > 0.8:
            break
        if n.bbox[1] - t.bbox[3] > 2.2 * t.size:
            break
        if t.text.rstrip().endswith((".", "?", "!")):
            break
        t.text = _join_text(t.text, n.text)
        t.bbox = (
            min(t.bbox[0], n.bbox[0]), min(t.bbox[1], n.bbox[1]),
            max(t.bbox[2], n.bbox[2]), max(t.bbox[3], n.bbox[3]),
        )
        del blocks[j]


def _merge_affiliations(blocks: list[Block]) -> list[Block]:
    """合并连续的单位/作者块（大型论文的作者单位列表常横跨整页）。"""
    out: list[Block] = []
    for b in blocks:
        if (
            out
            and out[-1].kind == b.kind
            and b.kind in ("affiliation", "author")
            and out[-1].page == b.page
        ):
            prev = out[-1]
            prev.text = _join_text(prev.text, b.text)
            prev.bbox = (
                min(prev.bbox[0], b.bbox[0]), min(prev.bbox[1], b.bbox[1]),
                max(prev.bbox[2], b.bbox[2]), max(prev.bbox[3], b.bbox[3]),
            )
            continue
        out.append(b)
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
    return out


def _classify(blocks: list[Block], body_size: float, page_height: float = 792.0) -> None:
    """按位置与文本特征标注块类型，并确定「不翻译」的块。"""
    in_refs = False
    section = ""
    front_matter = True          # 仍在首页标题区（Abstract 之前）
    body_started = False         # 已进入 Introduction，之后不再有作者单位
    title_idx = _pick_title_index(blocks, page_height)
    _merge_title_lines(blocks, title_idx)

    for i, b in enumerate(blocks):
        t = b.text.strip()

        if i == title_idx:
            b.kind = "title"
            b.level = 0
            continue

        if b.kind == "heading":
            # 首页 Abstract 之前的大字号块是作者/单位，不是章节标题
            if front_matter and b.page == 1 and not re.match(r"^(Abstract|摘\s*要)", t, re.IGNORECASE):
                if _NAME_RE.search(t) and len(t) < 300:
                    b.kind = "author"
                else:
                    b.kind = "affiliation"
                b.translatable = False
                b.level = 0
                continue
            section = t
            b.section = t
            if re.match(r"^(References|Bibliography)\b", t, re.IGNORECASE):
                in_refs = True
            if re.match(r"^(Abstract|摘\s*要)\b", t, re.IGNORECASE):
                front_matter = False
            if re.match(r"^(\d+\.?\s*)?(Introduction|摘\s*要)\b", t, re.IGNORECASE):
                body_started = True
            continue

        b.section = section

        # 公式/图/表已在构建阶段定型，不再改判
        if b.kind in ("equation", "figure", "table"):
            continue

        # Introduction 之前、长得像机构的块 → 作者单位（大型论文的单位列表会横跨两三页）
        if not body_started and len(t) < 320 and (
            _AFFIL_RE.search(t) or _AFFIL_MARKER_RE.match(t)
        ):
            b.kind = "affiliation"
            b.translatable = False
            continue

        if _KEYWORDS_RE.match(t):
            b.kind = "keywords"
            continue

        if in_refs:
            b.kind = "reference"
            b.translatable = False
            continue

        if front_matter and b.page == 1:
            if _AFFIL_RE.search(t) or len(t) > 260:
                b.kind = "affiliation"
            elif _NAME_RE.search(t) and t.count(",") >= 1:
                b.kind = "author"
            else:
                b.kind = "affiliation"
            b.translatable = False
            continue

        if _CAPTION_RE.match(t):
            b.kind = "caption"
        elif _FOOTNOTE_RE.search(t) and len(t) < 400:
            b.kind = "footnote"
            b.translatable = False
        else:
            b.kind = "para"


def _drop_noise(blocks: list[Block]) -> list[Block]:
    """丢弃页码、孤立标点、纯符号等碎片。"""
    out = []
    for b in blocks:
        t = b.text.strip()
        if not t:
            continue
        if len(t) <= 2 and not t.isalpha():
            continue
        if re.fullmatch(r"[\d\s\.\-–—]+", t):
            continue
        out.append(b)
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
    return out


_SENT_END_RE = re.compile(r"[.!?。！？][\"'”’)\]]*$")
_ABBREV_RE = re.compile(
    r"\b(?:et al|e\.g|i\.e|Fig|Tab|Eq|vs|etc|Dr|No|Ref|Sec|cf)\.$", re.IGNORECASE
)
_CANNOT_CONTINUE_RE = re.compile(
    r"^(?:however|moreover|furthermore|nevertheless|therefore|thus|hence|"
    r"in\s+addition|on\s+the\s+other\s+hand|meanwhile|consequently|finally|"
    r"first|second|third|overall|in\s+this\s+paper)\b",
    re.IGNORECASE,
)


def _ends_sentence(text: str) -> bool:
    t = text.rstrip()
    if not t:
        return True
    if _ABBREV_RE.search(t):        # "et al." / "Fig." 后面通常还接着正文
        return False
    return bool(_SENT_END_RE.search(t))


def _merge_across_pages(blocks: list[Block]) -> list[Block]:
    """续段合并：上一块没收句、后面的段落小写开头，视为同一段被切断。

    需要跨过中间的非正文块。典型场景是通栏大图把一段话劈成两半：

        第 1 页右栏末： "...in both structures are the same, as"
        第 2 页顶部：   [figure] [caption "Figure 1: ..."]
        第 2 页左栏首： "shown in (d), and the color of tail nodes ..."
    """
    skippable = {"figure", "table", "caption", "equation", "footnote"}
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        out.append(b)
        if b.kind in ("para", "caption") and not _ends_sentence(b.text):
            j = i + 1
            skipped: list[Block] = []
            while j < len(blocks) and blocks[j].kind in skippable and len(skipped) < 3:
                skipped.append(blocks[j])
                j += 1
            if j < len(blocks):
                n = blocks[j]
                if (
                    n.kind == "para"
                    and n.page in (b.page, b.page + 1)
                    and n.text[:1].islower()
                    and not _CANNOT_CONTINUE_RE.match(n.text)
                ):
                    b.text = _join_text(b.text, n.text)
                    b.bbox = (
                        min(b.bbox[0], n.bbox[0]), min(b.bbox[1], n.bbox[1]),
                        max(b.bbox[2], n.bbox[2]), max(b.bbox[3], n.bbox[3]),
                    )
                    out.extend(skipped)     # 图表等保持原位，接在合并后的段落之后
                    i = j + 1
                    continue
        i += 1
    return out


def _find_page_tables(doc: pymupdf.Document) -> dict[int, list[tuple[pymupdf.Rect, str]]]:
    """用 PyMuPDF 内置表格识别拿到结构化表格（HTML）。"""
    out: dict[int, list[tuple[pymupdf.Rect, str]]] = {}
    for pno in range(len(doc)):
        page = doc[pno]
        try:
            found = page.find_tables()
        except Exception:
            continue
        for tab in getattr(found, "tables", None) or []:
            try:
                rect = pymupdf.Rect(tab.bbox)
                if rect.width < 60 or rect.height < 18:
                    continue
                html = ""
                for meth in ("to_html", "to_markdown"):
                    if hasattr(tab, meth):
                        html = getattr(tab, meth)() or ""
                        break
                out.setdefault(pno + 1, []).append((rect, html))
            except Exception:
                continue
    return out


def _extract_tables_structured(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """优先用 find_tables 的结果：结构化 HTML + 裁剪图双份，并清掉表格区域的散碎文本。"""
    detected = _find_page_tables(doc)
    if not detected:
        return blocks

    insert_before: dict[str, list[Block]] = {}
    dropped: set[int] = set()

    for page_no, tables in detected.items():
        captions = [
            b for b in blocks
            if b.page == page_no and b.kind == "caption"
            and re.match(r"^(Table|TABLE|Tab\.)\s*\d", b.text)
        ]
        used: set[int] = set()
        for k, (rect, html) in enumerate(tables):
            best, best_d = None, 1e9
            for idx, c in enumerate(captions):
                if idx in used:
                    continue
                dist = min(abs(c.bbox[1] - rect.y1), abs(rect.y0 - c.bbox[3]))
                if dist < best_d and dist <= 160:
                    best, best_d = idx, dist
            if best is None:
                continue
            used.add(best)
            cap = captions[best]
            img = _crop(doc[page_no - 1], rect, out_dir / f"p{page_no:03d}_tab{k}.png")
            insert_before.setdefault(cap.id, []).append(
                Block(
                    id=f"tab_{page_no}_{k}", order=0, page=page_no, kind="table",
                    text="", bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                    image_path=img, caption=cap.text, table_html=html,
                    translatable=False,
                )
            )
            for b in blocks:
                if b.page != page_no or b.kind in ("figure", "table"):
                    continue
                cx = (b.bbox[0] + b.bbox[2]) / 2
                cy = (b.bbox[1] + b.bbox[3]) / 2
                if rect.x0 - 2 <= cx <= rect.x1 + 2 and rect.y0 - 2 <= cy <= rect.y1 + 2:
                    dropped.add(id(b))

    if not insert_before:
        return blocks

    result: list[Block] = []
    for b in blocks:
        if id(b) in dropped:
            continue
        if b.id in insert_before:
            result.extend(insert_before.pop(b.id))
        result.append(b)
    for i, blk in enumerate(result):
        blk.order = i
        blk.id = f"b{i:04d}"
    return result


def _extract_tables_by_caption(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """兜底：find_tables 没识别出来的表，以「Table N:」图注为锚点向上收集短块后整块裁图。"""
    result: list[Block] = []
    for b in blocks:
        if b.kind == "caption" and re.match(r"^(Table|TABLE|Tab\.)\s*\d", b.text):
            limit_y = b.bbox[1]
            idx = len(result) - 1
            region: list[int] = []
            while idx >= 0:
                p = result[idx]
                if p.page != b.page:
                    break
                if p.kind in ("figure", "table", "caption"):
                    break
                # 表头上的短"标题"（如 "Model"）属于表格，一并吸收
                if p.kind == "heading" and (len(p.text) > 40 or limit_y - p.bbox[3] > 24):
                    break
                if p.bbox[3] > limit_y + 14:
                    break
                if p.bbox[3] - p.bbox[1] > 48:      # 段落式高块，不是表格单元
                    break
                if limit_y - p.bbox[1] > 320:
                    break
                region.append(idx)
                idx -= 1
            if len(region) >= 2:
                rect = pymupdf.Rect(
                    min(result[k].bbox[0] for k in region) - 3,
                    min(result[k].bbox[1] for k in region) - 3,
                    max(result[k].bbox[2] for k in region) + 3,
                    max(result[k].bbox[3] for k in region) + 3,
                )
                img = _crop(
                    doc[b.page - 1], rect,
                    out_dir / f"p{b.page:03d}_table{len(region)}.png",
                )
                if img:
                    for k in sorted(region, reverse=True):
                        result.pop(k)
                    result.append(
                        Block(
                            id="", order=0, page=b.page, kind="table", text="",
                            bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                            image_path=img, caption=b.text, translatable=False,
                        )
                    )
        result.append(b)

    for i, blk in enumerate(result):
        blk.order = i
        blk.id = f"b{i:04d}"
    return result


def _drop_equation_orphans(blocks: list[Block]) -> list[Block]:
    """丢掉落在公式区域内的正文碎片。

    分段函数会被排版成「大括号 + 多行 case」，PyMuPDF 切出来的行宽窄不一，其中
    `1, otherwise.` 这类短行容易被判成正文，最后被翻译成「1，否则。」单独成段。
    判据：短、词少、且中心点落在某个公式块的包围盒内。
    """
    eqs = [b for b in blocks if b.kind == "equation"]
    if not eqs:
        return blocks

    out: list[Block] = []
    for b in blocks:
        if (
            b.kind == "para"
            and len(b.text) < 70
            and len(_WORD_RE.findall(b.text)) <= 6
        ):
            cx = (b.bbox[0] + b.bbox[2]) / 2
            cy = (b.bbox[1] + b.bbox[3]) / 2
            inside = any(
                e.page == b.page
                and e.bbox[0] - 8 <= cx <= e.bbox[2] + 8
                and e.bbox[1] - 8 <= cy <= e.bbox[3] + 8
                for e in eqs
            )
            if inside:
                continue
        out.append(b)

    for i, blk in enumerate(out):
        blk.order = i
        blk.id = f"b{i:04d}"
    return out


def _extract_tables(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """表格抽取：先做结构化识别，再对漏网的表走裁图兜底。"""
    blocks = _extract_tables_structured(doc, blocks, out_dir)
    return _extract_tables_by_caption(doc, blocks, out_dir)


# ── 图片 / 表格裁剪 ────────────────────────────────────────────────────

def _crop(page: pymupdf.Page, rect: pymupdf.Rect, path: Path, dpi: int = 170) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pix = page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
        pix.save(str(path))
        return str(path)
    except Exception:
        return None


def _extract_figures(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """把每页图片区域裁成 PNG，并按「图注」配对插入阅读顺序。"""
    insert_before: dict[str, list[Block]] = {}
    pending: dict[int, list[Block]] = {}

    for pno in range(len(doc)):
        page = doc[pno]
        page_no = pno + 1
        rects: list[pymupdf.Rect] = []

        # 1) 位图
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"])
            if r.width * r.height > 4000 and r.height > 40:
                rects.append(r)

        # 2) 矢量图：drawing 聚合成大区域，且区域内基本没有文字
        drawings = page.get_drawings()
        if drawings:
            dr = pymupdf.Rect()
            for d in drawings:
                r = pymupdf.Rect(d["rect"])
                if r.width > 20 and r.height > 20:
                    dr |= r
            if (
                not dr.is_empty
                and dr.width * dr.height > 9000
                and dr.height > 60
                and not _rect_has_text(page, dr)
            ):
                rects.append(dr)

        if not rects:
            continue

        captions = [b for b in blocks if b.page == page_no and b.kind == "caption"]
        for k, r in enumerate(rects):
            img = _crop(page, r, out_dir / f"p{page_no:03d}_fig{k}.png")
            if not img:
                continue
            fig = Block(
                id=f"fig_p{page_no}_{k}", order=0, page=page_no, kind="figure",
                text="", bbox=(r.x0, r.y0, r.x1, r.y1), image_path=img,
                translatable=False,
            )
            # 就近配对：图注通常紧贴图片下方
            cands = [
                c for c in captions
                if -10 <= c.bbox[1] - r.y1 <= 110
            ]
            if cands:
                cap = min(cands, key=lambda c: abs(c.bbox[1] - r.y1))
                fig.caption = cap.text
                insert_before.setdefault(cap.id, []).append(fig)
            else:
                pending.setdefault(page_no, []).append(fig)

    if not insert_before and not pending:
        return blocks

    result: list[Block] = []
    for b in blocks:
        if b.id in insert_before:
            result.extend(insert_before.pop(b.id))
        result.append(b)
    for page_no, figs in pending.items():
        idx = max(
            (i for i, b in enumerate(result) if b.page == page_no),
            default=len(result) - 1,
        )
        result[idx + 1:idx + 1] = figs

    for i, b in enumerate(result):
        b.order = i
        b.id = f"b{i:04d}"
    return result


def _rect_has_text(page: pymupdf.Page, rect: pymupdf.Rect) -> bool:
    """区域内文字占比是否显著（用于排除「文字区被误判成图」）。"""
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        r = pymupdf.Rect(b["bbox"])
        if r.intersects(rect) and (r & rect).get_area() > 0.15 * r.get_area():
            return True
    return False


# ── 主入口 ─────────────────────────────────────────────────────────────

def _ink_rect(page: pymupdf.Page, rect: pymupdf.Rect, pad: float = 4.0) -> pymupdf.Rect:
    """把公式块的外接矩形扩展到「实际墨迹」范围。

    文字 bbox 只覆盖字形主体：分式的分母、根号下沿、求和号的下标都会溢出。
    这里并入紧邻的字形与线条（分数线、根号横线），但用「字符中心是否落在
    rect±pad」来判断，避免把紧挨着的下一行正文裁进来。
    """
    out = pymupdf.Rect(rect)
    region = pymupdf.Rect(rect.x0 - 8, rect.y0 - pad, rect.x1 + 8, rect.y1 + pad)

    try:
        raw = page.get_text("rawdict", clip=region)
    except Exception:
        return out
    for blk in raw.get("blocks", []):
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    b = pymupdf.Rect(ch["bbox"])
                    if b.x1 < rect.x0 - 8 or b.x0 > rect.x1 + 8:
                        continue
                    cy = (b.y0 + b.y1) / 2
                    if not (rect.y0 - pad <= cy <= rect.y1 + pad):
                        continue
                    out |= b

    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    for d in drawings:
        r = pymupdf.Rect(d["rect"])
        if r.height > 90 or not r.intersects(region):
            continue
        if r.x1 < rect.x0 - 8 or r.x0 > rect.x1 + 8:
            continue
        out |= r

    return out


def _crop_equations(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
    dpi: int = 200,
) -> list[Block]:
    """把显示公式裁成图片：前端展示原式，避免把 `ˆD−1/2` 这类乱码直接给用户。"""
    for b in blocks:
        if b.kind != "equation" or b.image_path:
            continue
        rect = pymupdf.Rect(b.bbox)
        if rect.is_empty or rect.width < 2 or rect.height < 2:
            continue
        page = doc[b.page - 1]
        ink = _ink_rect(page, rect)
        b.image_path = _crop(page, ink, out_dir / f"eq_{b.order:04d}.png", dpi=dpi)
    return blocks


def parse_pdf(
    pdf_path: str | Path,
    paper_id: str = "",
    figure_dir: str | Path | None = None,
    with_figures: bool = True,
) -> ParsedPaper:
    """解析 PDF 为结构化块序列。

    Args:
        pdf_path: PDF 路径
        paper_id: 论文标识（用于图片命名隔离）
        figure_dir: 图片输出目录（None 则不裁剪图片）
        with_figures: 是否抽取图片

    Returns:
        ParsedPaper（blocks 已按阅读顺序排列）
    """
    pdf_path = Path(pdf_path)
    doc = pymupdf.open(str(pdf_path))
    try:
        lines = _collect_lines(doc)
        if not lines:
            return ParsedPaper(page_count=len(doc), blocks=[])

        _mark_margins(lines, doc)
        gutter = _detect_gutter(lines)
        _assign_columns(lines, gutter)
        rows = _merge_visual_rows(lines)      # 行内公式碎片并回正文行
        ordered = _reading_order(rows, gutter)

        body_size = _guess_body_size(ordered)
        blocks = _build_blocks(ordered, body_size)
        _classify(blocks, body_size, doc[0].rect.height)
        blocks = _merge_affiliations(blocks)
        blocks = _drop_noise(blocks)
        blocks = _drop_equation_orphans(blocks)
        blocks = _merge_across_pages(blocks)

        if with_figures and figure_dir is not None:
            out = Path(figure_dir)
            if paper_id:
                out = out / paper_id
            blocks = _extract_tables(doc, blocks, out / "tables")
            blocks = _extract_figures(doc, blocks, out / "figures")
            blocks = _crop_equations(doc, blocks, out / "equations")
        else:
            for i, b in enumerate(blocks):
                b.order = i
                b.id = f"b{i:04d}"

        return ParsedPaper(
            page_count=len(doc),
            blocks=blocks,
            body_size=body_size,
            columns=2 if gutter is not None else 1,
        )
    finally:
        doc.close()
