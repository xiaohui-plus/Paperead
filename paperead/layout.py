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
import pymupdf.table as _pymupdf_table


def _force_accurate_table_bboxes() -> bool:
    """让 find_tables 始终使用精确字符框。

    PyMuPDF 只在 `pymupdf.layout` 可导入时给表格检测加 TEXT_ACCURATE_BBOXES；
    发布包为控制体积没有打包该子包，运行时便退回粗略 bbox，把整页正文误判成表。
    """
    accurate = getattr(pymupdf, "TEXT_ACCURATE_BBOXES", None)
    original = getattr(_pymupdf_table, "textpage_flags", None)
    if accurate is None or original is None:
        return False

    def textpage_flags():
        return original() | accurate

    _pymupdf_table.textpage_flags = textpage_flags
    return True


_ACCURATE_TABLE_BBOXES = _force_accurate_table_bboxes()

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
# 图注 / 表注。编号既可能是阿拉伯数字（Table 3），也可能是罗马数字
# （MDPI/IEEE 常见的 TABLE II），还可能是补充材料编号（Table S1）。
_CAP_NUM = r"(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]\d+)"
_CAPTION_RE = re.compile(
    rf"^(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Figure|Fig\.|FIGURE|Table|TABLE|Tab\.)\s*{_CAP_NUM}\b",
    re.IGNORECASE,
)
_TABLE_CAP_RE = re.compile(
    rf"^(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Table|TABLE|Tab\.)\s*{_CAP_NUM}\b",
    re.IGNORECASE,
)
# 只包含公式编号的行：(3) / （3.1）。裸数字多是页码或图里的数据标签，不收
_EQ_NUM_ONLY_RE = re.compile(r"^[（(]\s*[A-Z]?\d{1,3}(?:\.\d+)?\s*[)）]$")


def _is_caption_text(text: str) -> bool:
    """区分「表题」和「正文里以 Table 1 开头的句子」。

    `Table 1 shows ...` 这种引用句也以 Figure/Table + 编号开头，但不是图注。
    真的图注在编号后通常紧跟冒号/句点，或者以大写标题开头。
    """
    t = text.strip()
    # 期刊图注经常带一长段说明（含 "Figure 1 shows..." 之外的完整描述），
    # 400 字符会把整条图注判成正文，图和注也就配不上了
    if not t or len(t) > 1000:
        return False
    m = _CAPTION_RE.match(t)
    if not m:
        return False
    after = t[m.end():]
    if after[:1] in (":", "：", ".", "-", "–", "—"):
        return True
    rest = after.strip()
    if not rest:
        return True
    first = rest[:1]
    # Nature 的图注格式是「Fig. 1 | Comparative ...」，竖线同样是编号分隔符
    if first in (":", "：", ".", "-", "–", "—", "|"):
        return True
    return first.isupper() or first.isdigit() or first in "（(“\"'["
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
    eq_number: str = ""             # 公式编号（PDF 里印出来的 (3) / (2.1)）
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
            "eq_number": self.eq_number,
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
    widths = {p: doc[p].rect.width for p in range(len(doc))}
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
                # 右边缘的「(2)」这类是公式编号，不是页码：每页公式编号会重复
                # 出现 (1)(2)...，归一化后都是 (##)，不能被当成页眉页脚丢掉
                if _EQ_NUM_ONLY_RE.match(ln.text.strip()):
                    w = widths.get(ln.page - 1, 612)
                    if ln.x1 > w * 0.6:
                        continue
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


_CONTINUE_AFTER = ",，、;；:-–—/"
_CONTINUE_START = re.compile(r"^(?:[a-z]|\d|\(|\[)")


def _continues_paragraph(prev: str, nxt: str) -> bool:
    """跨栏/跨页的两行是否属于同一段。

    只认明确的接续证据：上一段没有以句末标点结束，且本行以小写、
    数字或左括号开头，或上一行停在逗号/分号/连字符上。不确定就断开，
    宁可多一段，也不要把两段粘成一段。
    """
    prev = prev.rstrip().rstrip('”"’')
    nxt = nxt.lstrip()
    if not prev or not nxt:
        return False
    # 图注/表注是独立块：跨栏时上一段常停在逗号/连字符上，不能把
    # 「Figure 3: ...」并进正文，否则图注首行丢失、图片无法配对
    if _is_caption_text(nxt):
        return False
    if prev[-1] in ".!?。！？:：":
        return False
    if prev[-1] in _CONTINUE_AFTER:
        return True
    return bool(_CONTINUE_START.match(nxt))


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


_EQ_NUMBER_RE = re.compile(r"[（(]\s*([A-Z]?\d{1,3}(?:\.\d+)?)\s*[)）]\s*[.,;]?\s*$")


def _split_eq_number(text: str) -> tuple[str, str]:
    """把行尾右对齐的公式编号 `(3)` 从公式正文里拆出来。

    编号要单独渲染（前端右对齐），不能留在 LaTeX 里：视觉模型会把
    `(3)` 当成公式的一部分，导致 KaTeX 排版出一个漂移的编号。
    """
    m = _EQ_NUMBER_RE.search(text)
    if not m:
        return text, ""
    body = text[: m.start()].rstrip()
    if len(body) < 2:
        return text, ""
    return body, m.group(1)


def _build_blocks(ordered: list[Line], body_size: float) -> list[Block]:
    blocks: list[Block] = []
    cur: dict | None = None
    line_h = median([ln.height for ln in ordered]) if ordered else 12.0

    # 每页、每栏的左右边界，用于判断「行是否写满」与「是否首行缩进」。
    # 不能跨页取全局边界：首页通栏标题/摘要会把左栏的 right 撑到页面右边，
    # 导致后面双栏页的每一行都被误判成「没写满」而各起一段。
    edges: dict[tuple[int, int], tuple[float, float]] = {}
    for page in sorted({ln.page for ln in ordered}):
        for c in (0, 1):
            ls = [ln for ln in ordered if ln.page == page and ln.col == c]
            if ls:
                edges[(page, c)] = (
                    min(ln.x0 for ln in ls),
                    max(ln.x1 for ln in ls),
                )

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
            "size": ln.size, "bold": ln.bold, "col": ln.col, "mixed": False,
            "last_y1": ln.y1, "last_x0": ln.x0, "last_x1": ln.x1,
        }

    eq: dict | None = None

    def flush_eq():
        nonlocal eq
        if eq and eq["text"].strip():
            text, number = _split_eq_number(eq["text"].strip())
            blocks.append(
                Block(
                    id="", order=len(blocks), page=eq["page"], kind="equation",
                    text=text, bbox=eq["bbox"], translatable=False,
                    size=eq.get("size", 0.0), eq_number=number,
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

        # 上一行是否写满，要用「上一行自己所在栏」的边界判断；
        # 跨栏/跨页时如果拿新栏的右边界比，会误判成段落结束。
        prev_left, prev_right = edges.get(
            (cur["page"], cur["col"]), (cur["last_x0"], cur["last_x1"])
        )
        prev_short = cur["last_x1"] < prev_right - 0.08 * max(prev_right - prev_left, 1.0)
        left, right = edges.get((ln.page, ln.col), (ln.x0, ln.x1))
        # 首行缩进：既要相对栏左边界缩进，又要相对上一行缩进（否则整段每行都会被误判）
        indented = (
            ln.x0 - left > 0.8 * ln.size
            and ln.x0 - cur["last_x0"] > 0.5 * ln.size
        )
        gap = ln.y0 - cur["last_y1"]

        same_style = (
            abs(ln.size - cur["size"]) <= 1.0 and ln.bold == cur["bold"]
        )
        # 跨栏（左→右、右→下一页左栏）和跨页的断行：满足「上一行写满 +
        # 上一段没说完 + 本行小写/接续符开头 + 无首行缩进」就接回同一段。
        # 这是 Scholaread 强调的阅读连续性；只在有明确接续证据时合并，
        # 避免把相邻的两个段落粘在一起。
        cross = ln.page != cur["page"] or ln.col != cur["col"]
        continuation = (
            cross
            and same_style
            and not prev_short
            and not indented
            and _continues_paragraph(cur["text"], ln.text)
        )

        new_para = (not continuation) and (
            ln.col != cur["col"]
            or gap > max(line_h * 1.55, 6)
            or not same_style
            or prev_short
            or indented
        )
        if new_para:
            flush()
            cur = start(ln)
        else:
            cur["text"] = _join_text(cur["text"], ln.text)
            # 只在同页同栏内扩 bbox。跨栏/跨页续段用一个矩形取并集会
            # 覆盖整页（甚至两页），后续按 bbox 做的图表/公式排除会误伤。
            if ln.col != cur["col"]:
                cur["mixed"] = True
            if ln.page == cur["page"] and not cur["mixed"]:
                cur["bbox"] = (
                    min(cur["bbox"][0], ln.bbox[0]), min(cur["bbox"][1], ln.bbox[1]),
                    max(cur["bbox"][2], ln.bbox[2]), max(cur["bbox"][3], ln.bbox[3]),
                )
            cur["last_y1"] = ln.y1
            cur["last_x0"] = ln.x0
            cur["last_x1"] = ln.x1
            if ln.page == cur["page"]:
                cur["col"] = ln.col
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


_REF_START_RE = re.compile(r"^\s*\[?\d{1,3}\]?[.)]\s+\S")
_REF_YEAR_RE = re.compile(r"\b(?:19[5-9]\d|20[0-3]\d)\b")


def _looks_reference_entry(text: str) -> bool:
    """编号开头、且像文献条目（有年份/et al./DOI）而不是正文编号列表。"""
    if len(text) < 28 or not _REF_START_RE.match(text):
        return False
    return bool(
        _REF_YEAR_RE.search(text)
        or re.search(r"\bet\s+al\b|\bdoi\b|arxiv|https?://", text, re.IGNORECASE)
    )


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
            elif in_refs:
                # Nature 等 PDF 的参考文献区未必有可识别的 References 标题，
                # 进入下一个正常章节（Methods 等）时结束参考文献状态。
                in_refs = False
            if re.match(r"^(Abstract|摘\s*要)\b", t, re.IGNORECASE):
                front_matter = False
                # 很多期刊（如 Nature）没有 Introduction 标题；摘要之后就是正文，
                # 否则正文里的 "laboratory / Institute" 会被误判成作者单位。
                body_started = True
            if re.match(r"^(\d+\.?\s*)?(Introduction|摘\s*要)\b", t, re.IGNORECASE):
                body_started = True
            continue

        b.section = section

        # 公式/图/表已在构建阶段定型，不再改判
        if b.kind in ("equation", "figure", "table"):
            continue

        # Introduction 之前、长得像机构的块 → 作者单位（大型论文的单位列表会横跨两三页）。
        # 限制在前两页：有些期刊没有 Introduction 标题，后面的正文一旦出现
        # "laboratory / Institute" 会被误判成单位，整段就不再翻译。
        if not body_started and b.page <= 2 and len(t) < 320 and (
            _AFFIL_RE.search(t) or _AFFIL_MARKER_RE.match(t)
        ):
            b.kind = "affiliation"
            b.translatable = False
            continue

        if _KEYWORDS_RE.match(t):
            b.kind = "keywords"
            continue

        # 没有 References 标题的 PDF：用「1. 作者 et al. ... 2025」这种
        # 条目特征进入参考文献状态，后续条目保持 reference。
        if not in_refs and _looks_reference_entry(t):
            in_refs = True

        if in_refs:
            b.kind = "reference"
            b.translatable = False
            continue

        if front_matter and b.page == 1:
            # 首页底部的摘要经常没有 "Abstract" 标题，字号是正文字号、长度很长；
            # 不能当成作者单位，否则摘要不翻译、导读也拿不到内容。
            if b.size >= body_size * 0.95 and len(t) > 260:
                b.kind = "para"
                b.section = "Abstract"
                b.translatable = True
                front_matter = False
                body_started = True
                continue
            if _AFFIL_RE.search(t) or len(t) > 260:
                b.kind = "affiliation"
            elif _NAME_RE.search(t) and t.count(",") >= 1:
                b.kind = "author"
            else:
                b.kind = "affiliation"
            b.translatable = False
            continue

        if _CAPTION_RE.match(t) and _is_caption_text(t):
            b.kind = "caption"
        elif _FOOTNOTE_RE.search(t) and len(t) < 400:
            b.kind = "footnote"
            b.translatable = False
        else:
            b.kind = "para"


def _attach_equation_numbers(blocks: list[Block], doc: pymupdf.Document) -> list[Block]:
    """把右边缘独立的 `(3)` 行挂到同一水平线上的公式块上。

    双栏 PDF 里公式正文和编号常被切成两个 block（编号落在右栏边界），
    只靠行内文本拆不出来。这里按「同一基线 + 编号在公式右侧」配对，
    没有编号的公式保持为空，前端就不显示编号。
    """
    equations = [b for b in blocks if b.kind == "equation"]
    if not equations:
        return blocks

    widths = {p + 1: doc[p].rect.width for p in range(len(doc))}
    drop: set[int] = set()
    for b in blocks:
        if b.kind != "para" or not _EQ_NUM_ONLY_RE.match(b.text.strip()):
            continue
        width = widths.get(b.page, 612)
        if b.bbox[2] < width * 0.6:
            continue
        cy = (b.bbox[1] + b.bbox[3]) / 2
        cands = [
            e for e in equations
            if e.page == b.page
            and not e.eq_number
            and e.bbox[0] < b.bbox[0]
            and (e.bbox[1] + e.bbox[3]) / 2 - 10 <= cy <= (e.bbox[1] + e.bbox[3]) / 2 + 10
        ]
        if not cands:
            continue
        eq = min(cands, key=lambda e: abs((e.bbox[1] + e.bbox[3]) / 2 - cy))
        number = b.text.strip().strip("()（）").strip()
        if number:
            eq.eq_number = number
            drop.add(id(b))

    if not drop:
        return blocks
    out = [b for b in blocks if id(b) not in drop]
    for i, blk in enumerate(out):
        blk.order = i
        blk.id = f"b{i:04d}"
    return out


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
                    # 跨页续段无法用一个矩形表示；保留起始页的 bbox，
                    # 避免并集成整页/两页导致图表与公式的几何判断失效
                    out.extend(skipped)     # 图表等保持原位，接在合并后的段落之后
                    i = j + 1
                    continue
        i += 1
    return out


def _is_table_caption(text: str) -> bool:
    return bool(_TABLE_CAP_RE.match(text.strip())) and _is_caption_text(text)


def _merge_caption_continuations(blocks: list[Block]) -> list[Block]:
    """把紧贴图注的续行并回 caption。

    `_classify` 只按第一行识别图注，后面几行常被拆成独立 para：图注不完整、
    续行还会被当成正文，反过来影响「按图注裁图」的区域判断。这里只合并
    同页、紧贴（≤8pt）、左边界一致（±14pt）的段落，正文第一段不会满足。
    """
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind != "caption":
            out.append(b)
            i += 1
            continue
        j = i + 1
        while j < len(blocks):
            n = blocks[j]
            if n.kind != "para" or n.page != b.page:
                break
            gap = n.bbox[1] - b.bbox[3]
            if gap > 8 or abs(n.bbox[0] - b.bbox[0]) > 14 or len(b.text) > 600:
                break
            b.text = _join_text(b.text, n.text)
            b.bbox = (
                min(b.bbox[0], n.bbox[0]), min(b.bbox[1], n.bbox[1]),
                max(b.bbox[2], n.bbox[2]), max(b.bbox[3], n.bbox[3]),
            )
            j += 1
        out.append(b)
        i = j
    return out


def _overlap_ratio(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    """相交面积占较小矩形面积的比例（0~1）。"""
    inter = a & b
    if inter.is_empty:
        return 0.0
    small = min(a.width * a.height, b.width * b.height) or 1.0
    return inter.width * inter.height / small


def _rect_gap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    """两个矩形的最近间距；相交时为 0。"""
    dx = max(a.x0 - b.x1, b.x0 - a.x1, 0.0)
    dy = max(a.y0 - b.y1, b.y0 - a.y1, 0.0)
    return max(dx, dy)


def _page_tables(page: pymupdf.Page, strategy: str = "lines") -> list[tuple[pymupdf.Rect, str, int]]:
    """单页表格识别。lines 策略找有线框的表，text 策略找纯空白分隔的无框表。"""
    try:
        kwargs = {} if strategy == "lines" else {"strategy": strategy}
        found = page.find_tables(**kwargs)
    except Exception:
        return []
    rows: list[tuple[pymupdf.Rect, str, int]] = []
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
            rows.append((rect, html, int(getattr(tab, "col_count", 0) or 0)))
        except Exception:
            continue
    return rows


def _find_page_tables(doc: pymupdf.Document) -> dict[int, list[tuple[pymupdf.Rect, str, int]]]:
    """逐页找表：先用线框策略，没找到再用文本对齐策略。

    MDPI/IEEE 的综述表经常只有横线、列间靠空白对齐，`lines` 策略会整表漏掉；
    反过来 `text` 策略容易把双栏正文误判成表。所以按「lines 优先、text 补漏」
    两段式跑，并做一次重叠去重。
    """
    out: dict[int, list[tuple[pymupdf.Rect, str, int]]] = {}
    for pno in range(len(doc)):
        page = doc[pno]
        tables = _page_tables(page, "lines")
        if not tables:
            tables = _page_tables(page, "text")
        # 同一张表可能被两种策略各命中一次，或一张大表被切成几块：合并重叠项
        merged: list[tuple[pymupdf.Rect, str, int]] = []
        for rect, html, cols in tables:
            for i, (r0, h0, c0) in enumerate(merged):
                if _overlap_ratio(rect, r0) > 0.6:
                    merged[i] = (r0 | rect, h0 or html, max(c0, cols))
                    break
            else:
                merged.append((rect, html, cols))
        if merged:
            out[pno + 1] = merged
    return out


# 表格裁图向外吸收文字的最大水平间距（pt）。超过这个距离通常是
# 双栏页的另一栏正文，不能算进表格。
_TABLE_CROP_GAP = 24.0


def _table_crop_rect(page: pymupdf.Page, rect: pymupdf.Rect, margin: float = 4.0) -> pymupdf.Rect:
    """把表格识别框扩到「整张表」：包含整栏宽度和贴边的横线/表头。

    find_tables 的 bbox 常常从第一条单元格文字开始，漏掉左侧类别列和表头线；
    直接裁会缺边。这里按同纵向范围的文字边界扩到整栏，并入紧邻的细横线。
    """
    out = pymupdf.Rect(rect)
    x0, x1 = out.x0, out.x1
    try:
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            # 按行判断而不是按 block：同一 block 可能同时包含表格与
            # 另一栏正文，用 block bbox 会把两者当成相邻文字
            for ln in b.get("lines") or []:
                br = pymupdf.Rect(ln["bbox"])
                cy = (br.y0 + br.y1) / 2
                if not (out.y0 - 3 <= cy <= out.y1 + 3):
                    continue
                # 只吸收与识别框水平紧邻的单元格文字。双栏页里另一栏
                # 正文与表格隔着 gutter（约 27pt），按纵向范围无条件
                # 扩边会把整页正文裁进表格图，导致图表内容错位。
                gap = max(rect.x0 - br.x1, br.x0 - rect.x1, 0.0)
                if gap > _TABLE_CROP_GAP:
                    continue
                x0 = min(x0, br.x0)
                x1 = max(x1, br.x1)
    except Exception:
        pass
    page_rect = page.rect
    out = pymupdf.Rect(
        max(page_rect.x0, x0 - margin),
        max(page_rect.y0, out.y0 - margin),
        min(page_rect.x1, x1 + margin),
        min(page_rect.y1, out.y1 + margin),
    )
    try:
        for d in page.get_drawings():
            dr = pymupdf.Rect(d["rect"])
            if dr.height > 14:            # 只并入横线/分隔线，不并入整块图形
                continue
            if dr.x0 < out.x0 - 8 or dr.x1 > out.x1 + 8:
                continue
            if dr.y0 <= out.y0 + 10 and dr.y1 >= out.y0 - 10:
                out |= dr
            elif dr.y0 <= out.y1 + 10 and dr.y1 >= out.y1 - 10:
                out |= dr
    except Exception:
        pass
    return out


def _looks_like_text_table(html: str, cols: int) -> bool:
    """无框表格策略会把成段正文当成 1~2 行的表，靠行数/单元格长度区分。"""
    if cols < 2:
        return True
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", html or "", re.S)
    if not cells:
        return False
    rows = html.count("<tr")
    text = " ".join(re.sub(r"<[^>]+>", " ", c) for c in cells)
    text = re.sub(r"\s+", " ", text).strip()
    if rows > 2 or not text:
        return False
    return len(text) / len(cells) > 25


def _table_swallows_page(page_no: int, rect: pymupdf.Rect, blocks: list[Block]) -> bool:
    """表格区域若覆盖本页大部分正文，说明是 find_tables 把正文误判成表。"""
    covered = 0
    total = 0
    for b in blocks:
        if b.page != page_no or b.kind not in ("para", "heading"):
            continue
        n = len((b.text or "").strip())
        total += n
        cx = (b.bbox[0] + b.bbox[2]) / 2
        cy = (b.bbox[1] + b.bbox[3]) / 2
        if rect.x0 - 2 <= cx <= rect.x1 + 2 and rect.y0 - 2 <= cy <= rect.y1 + 2:
            covered += n
    return total >= 800 and covered >= total * 0.6


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
    insert_after: dict[str, list[Block]] = {}
    dropped: set[int] = set()
    last_cap: tuple[int, Block] | None = None

    for page_no in sorted(detected):
        page = doc[page_no - 1]
        captions = [
            b for b in blocks
            if b.page == page_no and b.kind == "caption" and _is_table_caption(b.text)
        ]
        used: set[str] = set()
        for k, (rect, html, _cols) in enumerate(detected[page_no]):
            if _looks_like_text_table(html, _cols):
                continue
            cap: Block | None = None
            best_d = 1e9
            for c in captions:
                if c.id in used:
                    continue
                dist = min(abs(c.bbox[1] - rect.y1), abs(rect.y0 - c.bbox[3]))
                if dist < best_d and dist <= 200:
                    cap, best_d = c, dist

            continued = False
            if cap is None and last_cap is not None:
                # 跨页续表：本页顶部就是表格，且上一页有表题 → 沿用上一页表题
                lp, last = last_cap
                if page_no - lp <= 1 and rect.y0 <= page.rect.height * 0.30:
                    cap, continued = last, True
            if cap is None:
                continue
            if _table_swallows_page(page_no, rect, blocks):
                continue
            if not continued:
                used.add(cap.id)
                last_cap = (page_no, cap)

            crop = _table_crop_rect(page, rect)
            img = _crop(
                page, crop, out_dir / f"p{page_no:03d}_tab{k}.png",
                target_px=TABLE_TARGET_PX,
            )
            if not img:
                continue
            label = f"{cap.text}（续）" if continued else cap.text
            blk = Block(
                id=f"tab_{page_no}_{k}", order=0, page=page_no, kind="table",
                text="", bbox=(crop.x0, crop.y0, crop.x1, crop.y1),
                image_path=img, caption=label, table_html=html,
                translatable=False,
            )
            # 表题在表上方（MDPI/IEEE）→ 表插到表题后面；表题在表下方 → 插到表题前面
            if cap.bbox[3] <= rect.y0:
                insert_after.setdefault(cap.id, []).append(blk)
            else:
                insert_before.setdefault(cap.id, []).append(blk)

            # 表格区域里的散碎文本已经画进裁图，不再作为正文/公式/图注留下
            for b in blocks:
                if b.page != page_no or b.kind in ("figure", "table", "caption"):
                    continue
                cx = (b.bbox[0] + b.bbox[2]) / 2
                cy = (b.bbox[1] + b.bbox[3]) / 2
                if rect.x0 - 2 <= cx <= rect.x1 + 2 and rect.y0 - 2 <= cy <= rect.y1 + 2:
                    dropped.add(id(b))

    if not insert_before and not insert_after:
        return blocks

    result: list[Block] = []
    for b in blocks:
        if id(b) in dropped:
            continue
        if b.id in insert_before:
            result.extend(insert_before.pop(b.id))
        result.append(b)
        if b.id in insert_after:
            result.extend(insert_after.pop(b.id))
    for i, blk in enumerate(result):
        blk.order = i
        blk.id = f"b{i:04d}"
    return result


def _has_table_neighbour(blocks: list[Block], idx: int) -> bool:
    """表题前后是否已经有 table 块（结构化识别成功的表）。"""
    for j in (idx - 1, idx + 1):
        if 0 <= j < len(blocks) and blocks[j].kind == "table":
            return True
    return False


def _collect_table_rows(
    blocks: list[Block], start: int, step: int, cap: Block
) -> list[int]:
    """从表题开始，向一个方向收集连续的「表格行」短块。

    表格行在版面里就是一行行短文本：高度小、间距紧、没有句末标点。
    碰到长段落、图表、另一个表题或间距明显变大就停止。
    """
    idx = start
    picked: list[int] = []
    edge = cap.bbox[1] if step < 0 else cap.bbox[3]
    while 0 <= idx < len(blocks):
        p = blocks[idx]
        if p.page != cap.page:
            break
        if p.kind in ("figure", "table", "caption", "equation", "reference", "footnote"):
            break
        gap = (edge - p.bbox[3]) if step < 0 else (p.bbox[1] - edge)
        if gap < -10 or gap > 42:
            break
        h = p.bbox[3] - p.bbox[1]
        if h > 60 or len(p.text.strip()) > 240:
            break
        picked.append(idx)
        edge = min(edge, p.bbox[1]) if step < 0 else max(edge, p.bbox[3])
        idx += step
        if len(picked) >= 80:
            break
    return picked


def _extract_tables_by_caption(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """兜底：find_tables 漏掉的表，以表题为锚点收集周围短块后整块裁图。

    表题可能在表上方（MDPI/IEEE）也可能在下方，因此上下都扫，取更像表格的一侧。
    """
    drop: set[int] = set()
    before: dict[int, list[Block]] = {}
    after: dict[int, list[Block]] = {}

    for i, b in enumerate(blocks):
        if b.kind != "caption" or not _is_table_caption(b.text):
            continue
        if _has_table_neighbour(blocks, i):
            continue

        up = _collect_table_rows(blocks, i - 1, -1, b)
        down = _collect_table_rows(blocks, i + 1, 1, b)
        picked = down if len(down) >= len(up) else up
        if len(picked) < 2:
            continue

        page = doc[b.page - 1]
        raw = pymupdf.Rect(
            min(blocks[k].bbox[0] for k in picked) - 3,
            min(blocks[k].bbox[1] for k in picked) - 3,
            max(blocks[k].bbox[2] for k in picked) + 3,
            max(blocks[k].bbox[3] for k in picked) + 3,
        )
        rect = _table_crop_rect(page, raw)
        if _table_swallows_page(b.page, rect, blocks):
            continue
        img = _crop(
            page, rect, out_dir / f"p{b.page:03d}_tbl_cap{i:04d}.png",
            target_px=TABLE_TARGET_PX,
        )
        if not img:
            continue
        tbl = Block(
            id="", order=0, page=b.page, kind="table", text="",
            bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
            image_path=img, caption=b.text, translatable=False,
        )
        # 表题在表上方 → 表放在表题后面；表题在下方（向上收集）→ 表放在表题前面
        if picked is down:
            after.setdefault(i, []).append(tbl)
        else:
            before.setdefault(i, []).append(tbl)
        drop.update(picked)

    if not before and not after:
        return blocks

    result: list[Block] = []
    for i, b in enumerate(blocks):
        if i in drop:
            continue
        if i in before:
            result.extend(before[i])
        result.append(b)
        if i in after:
            result.extend(after[i])

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


def _drop_equations_inside_floats(blocks: list[Block]) -> list[Block]:
    """丢掉落在图表裁图区域里的「公式」。

    图表里的坐标轴刻度、图例、流程图箭头旁的短标注经常是数学字体，
    会被版面解析误判成公式块，进而被裁成一张张碎图。它们已经包含在
    图表切片里了，留在阅读流里只会变成重复且看不懂的公式。
    """
    floats = [b for b in blocks if b.kind in ("figure", "table")]
    if not floats:
        return blocks

    out: list[Block] = []
    for b in blocks:
        if b.kind == "equation":
            cx = (b.bbox[0] + b.bbox[2]) / 2
            cy = (b.bbox[1] + b.bbox[3]) / 2
            inside = any(
                f.page == b.page
                and f.bbox[0] - 2 <= cx <= f.bbox[2] + 2
                and f.bbox[1] - 2 <= cy <= f.bbox[3] + 2
                for f in floats
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

# 裁图分辨率：阅读区正文宽约 700 CSS px，界面整体 120% 缩放后约 840 设备像素，
# 高分屏再乘 2。按「目标像素宽度」反算 DPI，比固定 170dpi 清晰得多，
# 也不会让整页大图无谓地放大到几十兆。
CROP_TARGET_PX = 1400
CROP_DPI_MIN = 200
CROP_DPI_MAX = 520
# 表格文字更小、列更多，按更高的目标宽度渲染，避免缩略后数字糊成一团
TABLE_TARGET_PX = 1800
EQUATION_TARGET_PX = 1200
EQUATION_DPI_MAX = 600


def _dpi_for(
    rect: pymupdf.Rect,
    target_px: int = CROP_TARGET_PX,
    hi: int = CROP_DPI_MAX,
    lo: int = CROP_DPI_MIN,
) -> int:
    """按目标像素宽度反算渲染 DPI（PDF 1pt = 1/72 英寸）。"""
    if rect.width <= 0:
        return lo
    return int(min(hi, max(lo, round(target_px * 72 / rect.width))))


def _crop(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    path: Path,
    dpi: int | None = None,
    target_px: int = CROP_TARGET_PX,
    dpi_max: int = CROP_DPI_MAX,
) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if dpi is None:
            dpi = _dpi_for(rect, target_px, dpi_max)
        pix = page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
        pix.save(str(path))
        return str(path)
    except Exception:
        return None


def _merge_rects(rects: list[pymupdf.Rect], gap: float = 4.0) -> list[pymupdf.Rect]:
    """合并重叠/紧邻的图形区域，避免同一张图被切成好几块重复切片。"""
    out: list[pymupdf.Rect] = []
    for r in sorted(rects, key=lambda x: (x.y0, x.x0)):
        for i, m in enumerate(out):
            if _overlap_ratio(r, m) > 0.5 or _rect_gap(r, m) <= gap:
                out[i] = m | r
                break
        else:
            out.append(pymupdf.Rect(r))

    # 链式合并：A 与 B 合并后可能又与 C 相交，重复扫描直到稳定
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                if _overlap_ratio(out[i], out[j]) > 0.5 or _rect_gap(out[i], out[j]) <= gap:
                    out[i] |= out[j]
                    out.pop(j)
                    changed = True
                    break
            if changed:
                break
    return out


def _merge_figure_regions(
    rects: list[pymupdf.Rect],
    gap_x: float = 30.0,
    gap_y: float = 16.0,
) -> list[pymupdf.Rect]:
    """把同一张图被拆成左右/上下子图的区域合并。

    期刊插图常由多个位图拼成（左半页/右半页、上下子图），get_image_info
    会给出多个 bbox；不合并就会出现「一张图配一个图注、另一张无注」。
    """
    merged: list[pymupdf.Rect] = []
    gap_x_th, gap_y_th = gap_x, gap_y
    for r in sorted(rects, key=lambda x: (x.y0, x.x0)):
        for i, m in enumerate(merged):
            overlap_y = (min(r.y1, m.y1) - max(r.y0, m.y0)) / max(
                min(r.height, m.height), 1
            )
            overlap_x = (min(r.x1, m.x1) - max(r.x0, m.x0)) / max(
                min(r.width, m.width), 1
            )
            gap_x = max(r.x0 - m.x1, m.x0 - r.x1, 0)
            gap_y = max(r.y0 - m.y1, m.y0 - r.y1, 0)
            if (overlap_y > 0.2 and gap_x <= gap_x_th) or (
                overlap_x > 0.2 and gap_y <= gap_y_th
            ):
                merged[i] = m | r
                break
        else:
            merged.append(r)
    return merged


def _figure_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """一页里的插图区域：位图 + 矢量图，合并去重后扩到图注/坐标轴标注。"""
    rects: list[pymupdf.Rect] = []
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"])
            # 4000pt² 会收进期刊 logo、二维码这类装饰图；8000 约等于
            # 90×90pt 的真实插图下限
            if r.width * r.height > 8000 and r.height > 40:
                rects.append(r)
    except Exception:
        pass
    rects = _merge_rects(rects)

    # 矢量图：drawing 聚合成大区域，且区域内基本没有文字
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
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
            rects = _merge_rects([*rects, dr])

    return [_figure_crop_rect(page, r) for r in _merge_figure_regions(rects)]


def _figure_crop_rect(page: pymupdf.Page, rect: pymupdf.Rect) -> pymupdf.Rect:
    """把图片框并到紧邻的矢量元素（坐标轴、图例框），保留少量留白。"""
    out = pymupdf.Rect(rect)
    region = pymupdf.Rect(rect.x0 - 8, rect.y0 - 8, rect.x1 + 8, rect.y1 + 8)
    try:
        for d in page.get_drawings():
            dr = pymupdf.Rect(d["rect"])
            if dr.is_empty or not dr.intersects(region):
                continue
            # 只吸收贴着图片的小元素，避免把整页装饰线吞进来
            if dr.width <= rect.width * 1.4 and dr.height <= rect.height * 1.4:
                out |= dr
    except Exception:
        pass
    return pymupdf.Rect(out.x0 - 2, out.y0 - 2, out.x1 + 2, out.y1 + 2)


def _extract_figures(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """把每页图片区域裁成 PNG，并按「图注」配对插入阅读顺序。"""
    insert_before: dict[str, list[Block]] = {}
    insert_after: dict[str, list[Block]] = {}
    pending: dict[int, list[Block]] = {}

    for pno in range(len(doc)):
        page = doc[pno]
        page_no = pno + 1
        table_rects = [
            pymupdf.Rect(b.bbox) for b in blocks
            if b.page == page_no and b.kind == "table"
        ]
        rects = _figure_rects(page)
        if not rects:
            continue

        captions = [
            b for b in blocks
            if b.page == page_no and b.kind == "caption" and not _is_table_caption(b.text)
        ]
        # 图区比图注多：多半是一张跨栏图被拆成左右子图，间距会超过
        # 常规的 30pt；按图注数量再合并一次，避免一半的图没有标题
        if 0 < len(captions) < len(rects):
            rects = _merge_figure_regions(rects, gap_x=42.0, gap_y=24.0)
        used: set[str] = set()
        for k, r in enumerate(rects):
            # 表格已经有独立切片，图片识别不能再把同一块区域当插图重复放一次
            if any(_overlap_ratio(r, t) > 0.25 for t in table_rects):
                continue
            img = _crop(page, r, out_dir / f"p{page_no:03d}_fig{k}.png")
            if not img:
                continue
            fig = Block(
                id=f"fig_p{page_no}_{k}", order=0, page=page_no, kind="figure",
                text="", bbox=(r.x0, r.y0, r.x1, r.y1), image_path=img,
                translatable=False,
            )
            # 就近配对：常见图注在图片下方，少数期刊在上方
            below = [
                c for c in captions
                if c.id not in used and -10 <= c.bbox[1] - r.y1 <= 200
            ]
            above = [
                c for c in captions
                if c.id not in used and -10 <= r.y0 - c.bbox[3] <= 150
            ]
            cap = None
            side = "below"
            if below:
                cap = min(below, key=lambda c: abs(c.bbox[1] - r.y1))
            elif above:
                cap = min(above, key=lambda c: abs(r.y0 - c.bbox[3]))
                side = "above"
            if cap:
                fig.caption = cap.text
                used.add(cap.id)
                if side == "below":
                    insert_before.setdefault(cap.id, []).append(fig)
                else:
                    insert_after.setdefault(cap.id, []).append(fig)
            else:
                pending.setdefault(page_no, []).append(fig)

    if not insert_before and not insert_after and not pending:
        return blocks

    result: list[Block] = []
    for b in blocks:
        if b.id in insert_before:
            result.extend(insert_before.pop(b.id))
        result.append(b)
        if b.id in insert_after:
            result.extend(insert_after.pop(b.id))
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
    dpi: int | None = None,
) -> list[Block]:
    """把显示公式裁成图片：前端展示原式，避免把 `ˆD−1/2` 这类乱码直接给用户。

    公式图还会送给识图模型转 LaTeX，所以分辨率比正文图表更高（默认按
    1200px 目标宽度反算，上限 600dpi）。公式区域很小，多出来的体积可以忽略。
    """
    for b in blocks:
        if b.kind != "equation" or b.image_path:
            continue
        rect = pymupdf.Rect(b.bbox)
        if rect.is_empty or rect.width < 2 or rect.height < 2:
            continue
        page = doc[b.page - 1]
        ink = _ink_rect(page, rect)
        b.image_path = _crop(
            page,
            ink,
            out_dir / f"eq_{b.order:04d}.png",
            dpi=dpi,
            target_px=EQUATION_TARGET_PX,
            dpi_max=EQUATION_DPI_MAX,
        )
    return blocks


def _is_body_block(b: Block) -> bool:
    """是否为正文块：章节标题，或成段的段落文本。

    图里的短标注（"Model Family"、"Rz Rz"）长度都在几个到二十几个字符，
    而且常被版式判成 heading；只有足够长的标题才按正文保留，否则当成
    图内标注随裁图一起收掉。
    """
    if b.kind == "heading":
        return len(b.text.strip()) > 35
    if b.kind != "para":
        return False
    return len(b.text.strip()) > 80


def _extract_vector_text_figures(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """带文字标注的矢量图：按图注裁出图形区域，正文块保持原样。

    `_extract_figures` 只认「区域内没有文字」的矢量图；而期刊里的散点图
    矩阵、系统图整页都是短标注，会被 `_collapse_vector_figure_pages` 整页
    图化，连页内正文一起吞掉。这里先按图注把图区裁出来，只删除落在图区
    里的短标注，正文和图注继续按块渲染。
    """
    by_page: dict[int, list[tuple[int, Block]]] = {}
    for i, b in enumerate(blocks):
        by_page.setdefault(b.page, []).append((i, b))

    insert_before: dict[str, list[Block]] = {}
    insert_after: dict[str, list[Block]] = {}
    drop: set[str] = set()

    for page_no, items in by_page.items():
        if page_no < 1 or page_no > len(doc):
            continue
        page = doc[page_no - 1]
        if any(b.kind == "figure" for _, b in items):
            continue
        if page.get_images(full=True) or len(page.get_drawings()) < 200:
            continue

        texts = [
            b for _, b in items
            if b.kind not in ("figure", "table", "reference") and b.text.strip()
        ]
        if len(texts) < 8:
            continue
        # 图注续行也算长块，所以只统计真正的正文段落：正文段落多说明这是
        # 正文页，不该按图裁；图注再长也不影响判断
        long = sum(
            1 for b in texts if _is_body_block(b) and len(b.text.strip()) > 220
        )
        if long > 4:
            continue

        bodies = [b for b in texts if _is_body_block(b)]
        if not bodies:
            continue  # 纯图表页交给整页图化兜底
        caps = sorted(
            [b for b in texts if b.kind == "caption" and not _is_table_caption(b.text)],
            key=lambda b: b.bbox[1],
        )
        if not caps:
            continue
        body_rects = [pymupdf.Rect(b.bbox) for b in bodies] + [
            pymupdf.Rect(b.bbox) for _, b in items if b.kind == "table"
        ]

        marks = [
            b for b in texts
            if b.kind not in ("caption", "equation") and not _is_body_block(b)
        ]
        if len(marks) < 3:
            continue

        # 判断图注在图上方还是下方：主流排版图注在图下方；只有页面开头
        # 就是图注时才反过来。按方向划分区域，避免把相邻图的标注配错。
        caption_on_top = caps[0].bbox[1] < min(b.bbox[1] for b in marks)

        def marks_between(lo: float, hi: float) -> list[Block]:
            found: list[Block] = []
            for b in marks:
                cy = (b.bbox[1] + b.bbox[3]) / 2
                if lo - 4 <= cy <= hi + 4:
                    found.append(b)
            return found

        prev_y = 0.0
        for idx, cap in enumerate(caps):
            next_y = caps[idx + 1].bbox[1] if idx + 1 < len(caps) else page.rect.height
            if caption_on_top:
                fig_marks = marks_between(cap.bbox[3], next_y)
                side = "below"
            else:
                fig_marks = marks_between(prev_y, cap.bbox[1])
                side = "above"
            prev_y = cap.bbox[3] + 2
            if len(fig_marks) < 3:
                continue
            # 图内标注都很短；平均过长说明把图注续行/正文行当成标注了
            if sum(len(b.text.strip()) for b in fig_marks) / len(fig_marks) > 60:
                continue

            rect = pymupdf.Rect()
            for b in fig_marks:
                rect |= pymupdf.Rect(b.bbox)
            # 并入与图区相交/紧邻的线条（坐标轴、图例框），不碰正文区
            for d in page.get_drawings():
                dr = pymupdf.Rect(d["rect"])
                if dr.is_empty or dr.height <= 1:
                    continue
                if _overlap_ratio(dr, rect) > 0 or _rect_gap(dr, rect) <= 10:
                    rect |= dr
            rect = pymupdf.Rect(
                max(page.rect.x0, rect.x0 - 4),
                max(page.rect.y0, rect.y0 - 4),
                min(page.rect.x1, rect.x1 + 4),
                min(page.rect.y1, rect.y1 + 4),
            )
            # 图形区域不能压到正文块，否则说明图注定位错了
            if any(_overlap_ratio(rect, br) > 0.15 for br in body_rects):
                continue
            if rect.width < 60 or rect.height < 40:
                continue

            img = _crop(page, rect, out_dir / f"p{page_no:03d}_vec{idx}.png")
            if not img:
                continue
            fig = Block(
                id=f"vec_p{page_no}_{idx}", order=0, page=page_no, kind="figure",
                text="", bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                image_path=img, caption=cap.text, translatable=False,
            )
            if side == "above":
                insert_before.setdefault(cap.id, []).append(fig)
            else:
                insert_after.setdefault(cap.id, []).append(fig)
            drop.update(b.id for b in fig_marks)

    if not insert_before and not insert_after:
        return blocks

    result: list[Block] = []
    for b in blocks:
        if b.id in drop:
            continue
        if b.id in insert_before:
            result.extend(insert_before.pop(b.id))
        result.append(b)
        if b.id in insert_after:
            result.extend(insert_after.pop(b.id))
    for i, b in enumerate(result):
        b.order = i
        b.id = f"b{i:04d}"
    return result


def _collapse_vector_figure_pages(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """把纯矢量图表页收敛成一张整页图，避免把图里的标注拆成一句一块。

    Nature 这类 PDF 的系统图/流程图没有位图，只有几百条矢量线段和大量
    独立短文本。它们不是正文段落，逐块翻译既没有意义，也会出现
    「一句一块」的观感。这里对「矢量元素很多 + 文本几乎都是短标注」的页面
    直接渲染成 PNG，替换掉该页的文字块。
    """
    by_page: dict[int, list[tuple[int, Block]]] = {}
    for i, b in enumerate(blocks):
        by_page.setdefault(b.page, []).append((i, b))

    replacements: dict[int, tuple[int, Block]] = {}
    for page_no, items in by_page.items():
        if page_no < 1 or page_no > len(doc):
            continue
        page = doc[page_no - 1]
        # 已经有位图/表格块的页面交给原来的图表抽取逻辑，避免重复
        if any(b.kind in ("figure", "table") for _, b in items):
            continue
        if page.get_images(full=True) or len(page.get_drawings()) < 200:
            continue

        texts = [
            (i, b) for i, b in items
            if b.kind not in ("figure", "table", "reference") and b.text.strip()
        ]
        if len(texts) < 8:
            continue
        short = sum(1 for _, b in texts if len(b.text.strip()) <= 70)
        long = sum(1 for _, b in texts if len(b.text.strip()) > 220)
        if short < len(texts) * 0.7 or long > 2:
            continue
        # 页面里只要还有成段的正文，就不能整页图化：整页替换会把这些正文
        # 一起吞进图片（引言、附录说明、图下的分析段都遇到过）。交给常规的
        # 图表抽取，只把矢量图形裁成 figure，正文继续按段落渲染。
        if any(
            b.kind in ("para", "heading") and len(b.text.strip()) > 80
            for _, b in texts
        ):
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        img = out_dir / f"page_{page_no:03d}.png"
        page.get_pixmap(dpi=_dpi_for(page.rect, CROP_TARGET_PX, 300)).save(str(img))
        first = min(i for i, _ in texts)
        # 用页面上真实的图注，而不是笼统的「第 N 页图表」；没有图注才兜底
        caps = [b.text.strip() for _, b in items if b.kind == "caption" and b.text.strip()]
        replacements[page_no] = (
            first,
            Block(
                id="",
                order=0,
                page=page_no,
                kind="figure",
                text="",
                bbox=(0.0, 0.0, page.rect.width, page.rect.height),
                image_path=str(img),
                caption=caps[0] if caps else f"第 {page_no} 页图表",
                translatable=False,
            ),
        )

    if not replacements:
        return blocks

    out: list[Block] = []
    for i, b in enumerate(blocks):
        repl = replacements.get(b.page)
        if repl is not None:
            if i == repl[0]:
                out.append(repl[1])
            continue
        out.append(b)
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
    return out


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
        # 按页检测栏间距：同一 PDF 里经常混有单栏首页、双栏正文、
        # 通栏图表页。用整篇文档的全局投影会被图表页的跨栏文字填满 gutter，
        # 最终误判成单栏，正文左右栏就会交错、且每行各起一段。
        pages: dict[int, list[Line]] = {}
        for ln in lines:
            pages.setdefault(ln.page, []).append(ln)
        gutters: dict[int, float | None] = {}
        for page_no, page_lines in pages.items():
            page_gutter = _detect_gutter(page_lines)
            gutters[page_no] = page_gutter
            _assign_columns(page_lines, page_gutter)
        has_gutter = any(g is not None for g in gutters.values())
        rows = _merge_visual_rows(lines)      # 行内公式碎片并回正文行
        ordered = _reading_order(rows, 0.0 if has_gutter else None)

        body_size = _guess_body_size(ordered)
        blocks = _build_blocks(ordered, body_size)
        _classify(blocks, body_size, doc[0].rect.height)
        blocks = _attach_equation_numbers(blocks, doc)
        blocks = _merge_affiliations(blocks)
        blocks = _drop_noise(blocks)
        blocks = _drop_equation_orphans(blocks)
        blocks = _merge_across_pages(blocks)
        blocks = _merge_caption_continuations(blocks)

        if with_figures and figure_dir is not None:
            out = Path(figure_dir)
            if paper_id:
                out = out / paper_id
            blocks = _extract_tables(doc, blocks, out / "tables")
            blocks = _extract_figures(doc, blocks, out / "figures")
            # 带文字标注的矢量图先按图注裁出图形区域，避免后面的整页图化
            # 把同一页的正文一起吞进图片
            blocks = _extract_vector_text_figures(doc, blocks, out / "figures")
            blocks = _collapse_vector_figure_pages(doc, blocks, out / "figures")
            # 图表裁好之后再清一次：图里的坐标轴/标注不该再作为公式出现
            blocks = _drop_equations_inside_floats(blocks)
            blocks = _crop_equations(doc, blocks, out / "equations")
        else:
            for i, b in enumerate(blocks):
                b.order = i
                b.id = f"b{i:04d}"

        return ParsedPaper(
            page_count=len(doc),
            blocks=blocks,
            body_size=body_size,
            columns=2 if has_gutter else 1,
        )
    finally:
        doc.close()
