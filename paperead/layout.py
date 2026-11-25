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

import json
import html as html_lib
import os
import re
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import pymupdf
import pymupdf.table as _pymupdf_table

from .fonts.registry import is_anchor_font
from .inline_math import (
    SYMBOLS,
    build_inline_math,
    build_symbol_fallbacks,
    spans_with_offsets,
    tex_brace_delta,
    tex_braces_balanced,
)
from .regions import PageRegions, build_page_regions
from .textnorm import (
    has_dot_leader,
    is_dot_leader_only,
    keep_space_between,
    squeeze_cjk_spaces,
    strip_dot_leaders,
)


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

# 解析中间量诊断：PAPEREAD_DIAG 指向文件时，parse_pdf 结束前把行几何、
# 栏归属、表格区与分块结果写到该 JSON。发布包与源码解析结果不一致时，
# 靠这份数据定位第一个分叉的量，而不是逐条猜判据。默认关闭。
_DIAG_PATH = os.environ.get("PAPEREAD_DIAG", "")


def _diag_dump(payload: dict) -> None:
    if not _DIAG_PATH:
        return
    try:
        out = Path(_DIAG_PATH)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # 诊断本身不能影响正常解析
        pass

# ── 文本清洗 ───────────────────────────────────────────────────────────

# LaTeX 断词：行尾连字符（含 Unicode 连字符 U+2010/U+2011 与软连字符）
_HYPHEN_END = re.compile(r"([A-Za-z])[-\u00ad\u2010\u2011]\s*$")

# 排版连字与不间断空格。PDF 常把 ﬁ/ﬂ 作为单个字形编码，原样保留会让
# 「difﬁcult」搜不到「difficult」，翻译与高亮也对不上。
_LIGATURES = str.maketrans({
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "ft", "\ufb06": "st",
    "\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u202f": " ",
    "\t": " ",
})

# PDF 里的 C0 控制字符（换行/回退等）不应进入正文。CMEX10 的大括号延伸
# 片段用的是 0x08/0x09，须在 `_line_from_dict` 里先按字体映射，再走这里兜底。
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _normalize_text(text: str) -> str:
    """把排版连字换成等价字母、把特殊空格换成普通空格，其余字符原样保留。"""
    return _CONTROL_CHARS_RE.sub("", text.translate(_LIGATURES))
# 章节标题（行首，允许前面有编号）
_SECTION_RE = re.compile(
    r"^(?P<head>(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(?:Abstract|Introduction|Related Works?|Background|Preliminaries?|"
    r"Materials? and Methods|Methods?|Approach|Model|Experiments?|"
    r"Results?|Discussion|Conclusions?|References|Bibliography|"
    r"Acknowledg(?:e)?ments?|Appendix(?:\s+[A-Z])?)\b)\s*",
    re.IGNORECASE,
)
# 论文后置声明区小标题（BMC/Springer 等常见），常常只有正文字号、
# 不加粗、也不带编号；靠词表与短行识别，避免整段声明被当正文。
_DECLARATION_HEAD_RE = re.compile(
    r"^(?:Authors?'?\s+contributions|Author contributions|Funding|"
    r"Availability of data(?:\s+and\s+materials)?|"
    r"Ethics approval(?:\s+and\s+consent\s+to\s+participate)?|"
    r"Consent for publication|Competing interests|Conflict of interest|"
    r"Acknowledg(?:e)?ments?|Declarations|Abbreviations|List of abbreviations|"
    r"Publisher['’]s Note|Supplementary information|Additional information)\b",
    re.IGNORECASE,
)

# 编号小节标题：2.1 Foo Bar
_NUM_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z]")
# 中文小标题（「2.2 智能问答性能」）没有大小写、也常常不加粗，只能靠
# 「编号 + 空格 + 汉字」的形态识别；长度由调用方限制，避免误伤正文。
_NUM_HEADING_ZH_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s*")
# 编号后面紧跟这些量词时是数据短语（「3.2 分」「15 秒」），不是标题。
_ZH_MEASURE_RE = re.compile(r"^(?:倍|个|次|分|米|秒|年|月|日|%|％)")
# 图注 / 表注。编号既可能是阿拉伯数字（Table 3），也可能是罗马数字
# （MDPI/IEEE 常见的 TABLE II），还可能是补充材料编号（Table S1）。
_CAP_NUM = r"(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]\d+|(?-i:[A-Z])\.\d+)"
# 编号后不能紧跟字母/数字/汉字：`表2所示` 是正文引用，不是表题。
_CAP_END = r"(?![0-9A-Za-z\u3400-\u9fff])"
# Algorithm 也是漂浮在正文之外的编号浮框，题注形态与图表一致：
# 「Algorithm 1 Preference-centric knowledge diffusion.」标题行即它的表头。
_CAP_EN_PREFIX = r"(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Figure|Fig\.|FIGURE|Table|TABLE|Tab\.|Algorithm|ALGORITHM)"
_CAP_ZH_PREFIX = r"(?:附\s*)?[图表]"
_CAPTION_RE = re.compile(
    rf"^(?:{_CAP_EN_PREFIX}\s*{_CAP_NUM}|{_CAP_ZH_PREFIX}\s*{_CAP_NUM}){_CAP_END}",
    re.IGNORECASE,
)
_TABLE_CAP_RE = re.compile(
    rf"^(?:(?:(?:Supplementary|Extended\s+Data)\s+)?(?:Table|TABLE|Tab\.)\s*{_CAP_NUM}"
    rf"|(?:Algorithm|ALGORITHM)\s*{_CAP_NUM}"
    rf"|(?:附\s*)?表\s*{_CAP_NUM}){_CAP_END}",
    re.IGNORECASE,
)
# 只有编号、没有标题内容的图注/表注（"TABLE I" / "Figure 3" / "图 3"）。
# 这类注的标题通常另起一行且居中，需要单独放宽续行合并条件。
_CAPTION_NUM_ONLY_RE = re.compile(
    rf"^(?:{_CAP_EN_PREFIX}\s*{_CAP_NUM}|{_CAP_ZH_PREFIX}\s*{_CAP_NUM})"
    rf"\s*[:：\.\-–—|]?$",
    re.IGNORECASE,
)
# 算法浮框：题注只取标题行，`Input:` 等伪代码首行不能并进表头。
_ALG_CAPTION_RE = re.compile(r"^(?:Algorithm|ALGORITHM)\b")
_ALG_BODY_LINE_RE = re.compile(
    r"^(?:Input|Output|Require|Ensure|Procedure|Function|Data|Result)\b\s*[:：]"
    r"|^\d+\s*[:：.]",
    re.IGNORECASE,
)
# 中文编号后若是这些词开头，是正文里的「如表 2 所示」而不是表题。
_CAPTION_ZH_REF_RE = re.compile(
    r"^(?:所示|中|里|列出|给出|展示|显示|表明|说明|可见|对比|比较|为|是|与|和|、|，|,)"
)
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# 只包含公式编号的行：(3) / （3.1）。裸数字多是页码或图里的数据标签，不收
_EQ_NUM_ONLY_RE = re.compile(r"^[（(]\s*[A-Z]?\d{1,3}(?:\.\d+)?\s*[)）]$")


def _aligns_column_edge(xs: list[float], x: float, tol: float = 5.0) -> bool:
    """x 是否贴着某一条栏的右边界（xs 为该页非编号行的右边界集合）。

    双栏 PDF 里公式编号贴在**栏**右缘，而不是页面右缘：左栏编号的 x1 通常在
    页面宽度 48% 左右，用整页宽度的 0.6 判断会整批漏掉。
    """
    i = bisect_left(xs, x - tol)
    return i < len(xs) and xs[i] <= x + tol


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
    # 中文没有大小写可以判断：编号后紧跟 CNY 标题一般是图注/表注，
    # 但「如表 2 所示」「图 3 中」这类是正文引用，必须排除。
    if _CJK_RE.match(first):
        return not _CAPTION_ZH_REF_RE.match(rest)
    # `Fig. 1 a Representation ...` 是期刊常见的子图标注格式：
    # 编号后先出现小写子图字母，再跟标题正文。
    if re.match(r"^[a-z]\s+[A-Z]", rest):
        return True
    # `Fig. 2(B)–(C), we introduce ...` 是正文里的跨子图引用句，不是图注：
    # 编号后紧跟一组括号子图标记，再以逗号/连接号接一个小写主语或动词。
    if first in "(（" and re.match(
        r"^[（(][^)）]{1,8}[)）]"
        r"(?:\s*(?:[-–—,;]|and|or|to)\s*[（(][^)）]{1,8}[)）])*"
        r"\s*[-–—,;]\s*(?:we|the|this|these|it|they|its|our|as|in|for)\b",
        rest,
        re.IGNORECASE,
    ):
        return False
    return first.isupper() or first.isdigit() or first in "（(“\"'["

# CMEX10 把大括号画成 0x08/0x09 的延伸部件；不映射就会在正文里变成
# 控制字符，集合语义（{x_t^front, x_t^down}）随之丢失。
_CMEX_BRACES = {"\x08": "{", "\x09": "}"}

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
    inline_math: list[dict] = field(default_factory=list)  # 正文里的行内公式片段
    brace_piece: bool = False  # CMEX 大括号延伸片段（bbox 跨行，不能按基线归行）


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
    inline_math: list[dict] = field(default_factory=list)  # 正文行内公式（start/end/latex）
    # 该块实际文字行矩形（逐页：page, x0, y0, x1, y1）。渲染层只读它，
    # 不再用 bbox + 扩栏/收窄/下推去猜「这一段到底占哪里」。
    text_rects: list[tuple] = field(default_factory=list)
    translatable: bool = True
    translated: str = ""
    # 跨栏/跨页块的逐页矩形 (page, x0, y0, x1, y1)。单矩形 bbox 取并集会覆盖
    # 整页甚至两页，高亮与「跳到原文」都会落错位置，这里保留每页真实范围。
    bboxes: list[tuple] = field(default_factory=list)
    # 列角色：body（正文，可译）/ sidebar（页缘信息栏，白名单保留）/ ""（未判定）。
    column_role: str = ""

    def to_dict(self) -> dict:
        data = {
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
            "column_role": self.column_role,
            "bbox": [round(v, 1) for v in self.bbox],
        }
        if self.inline_math:
            data["inline_math"] = self.inline_math
        if len(self.bboxes) > 1:
            data["bboxes"] = [
                [int(b[0]), *(round(v, 1) for v in b[1:])] for b in self.bboxes
            ]
        if self.text_rects:
            data["text_rects"] = [
                [int(r[0]), *(round(v, 1) for v in r[1:])]
                for r in self.text_rects
            ]
        return data


def _push_bbox(bboxes: list[list[float]], page: int, rect) -> None:
    """把一行的矩形并入该页的并集矩形；不同页各留一项。"""
    for item in bboxes:
        if item[0] == page:
            item[1] = min(item[1], rect[0])
            item[2] = min(item[2], rect[1])
            item[3] = max(item[3], rect[2])
            item[4] = max(item[4], rect[3])
            return
    bboxes.append([page, rect[0], rect[1], rect[2], rect[3]])


def _merge_bbox_lists(base: list, extra: list) -> list:
    """合并两个逐页 bbox 列表（段落/标题/图注跨页合并时用）。"""
    out = [list(x) for x in base]
    for item in extra:
        _push_bbox(out, int(item[0]), item[1:])
    return out


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
    math = is_anchor_font(font)
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
                and keep_space_between(last, t)
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
    # CMEX 的大括号片段：把 0x08/0x09 还原成 { / }，并标记为跨行片段，
    # 供 `_merge_visual_rows` 按 bbox 重叠而不是基线归行。映射必须放在
    # 「过滤空文本」之前：0x09 本身是制表符，strip 后会被当成空 span 丢掉。
    raw_spans = raw.get("spans") or []
    brace_piece = any(
        (s.get("font") or "").startswith("CMEX")
        and (s.get("text") or "") in _CMEX_BRACES
        for s in raw_spans
    )
    spans: list[dict] = []
    for span in raw_spans:
        text = span.get("text") or ""
        if (span.get("font") or "").startswith("CMEX") and text in _CMEX_BRACES:
            span = {**span, "text": _CMEX_BRACES[text]}
        if (span.get("text") or "").strip():
            spans.append(span)
    if not spans:
        return None
    # 连字（ﬃ → ffi）与控制字符必须在计算偏移前展开：`_normalize_text`
    # 会改变文本长度，若先算 ranges 再 normalize，后续行内公式/异常符号的
    # start/end 会整体错位（实测会把相邻正文裁进公式小图）。
    spans = [
        {**span, "text": _normalize_text(span.get("text") or "")}
        for span in spans
    ]
    spans = [span for span in spans if span["text"].strip()]
    if not spans:
        return None
    text, ranges = spans_with_offsets(spans)
    text, offsets = squeeze_cjk_spaces(text)
    if offsets != list(range(len(text) + 1)):
        ranges = [(offsets[start], offsets[end], span) for start, end, span in ranges]
    if not text.strip():
        return None
    inline_math = [m.to_dict() for m in build_inline_math(spans, ranges)]
    # 私用区/替换符没有可用字形（前端显示成「□」），又不在数学字体组里时，
    # 单独标成 fallback：能识图就恢复 LaTeX，识不了就用原 PDF 裁图垫后。
    inline_math += [
        m.to_dict() for m in build_symbol_fallbacks(text, ranges, inline_math)
    ]
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
        inline_math=inline_math,
        brace_piece=brace_piece,
    )


def _merge_visual_lines(raw_lines: list[dict]) -> list[dict]:
    """把 PyMuPDF 拆开的上下标行并回同一视觉行。

    PDF 里 `\\mathcal{P}^{query}_{...}` 的上标/下标基线不同，PyMuPDF 会拆成
    独立 line；不合并的话，行内公式只能看到半截。
    """
    spans = [
        s for line in raw_lines
        for s in line.get("spans") or []
        if (s.get("text") or "").strip()
    ]
    if len(raw_lines) <= 1 or not spans:
        return raw_lines
    prose = [
        s for s in spans
        if not is_anchor_font(s.get("font") or "")
    ]
    if not prose:
        return raw_lines

    base = max(float(s.get("size") or 0) for s in prose) or 10.0
    anchors: list[float] = []
    for span in prose:
        y = float((span.get("origin") or (0, 0))[1])
        if any(abs(y - anchor) <= max(1.2, base * 0.6) for anchor in anchors):
            continue
        anchors.append(y)

    groups: dict[float, list[dict]] = {round(a, 1): [] for a in anchors}
    leftovers: list[dict] = []
    for span in spans:
        y = float((span.get("origin") or (0, 0))[1])
        nearest = min(anchors, key=lambda a: abs(a - y), default=None)
        if nearest is not None and abs(nearest - y) <= base * 0.95:
            groups[round(nearest, 1)].append(span)
        else:
            leftovers.append(span)

    out: list[dict] = []
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda s: s["bbox"][0])
        # 上下标常与基础符号的 x 重叠（P、query、SPD 的 x0 只差 1pt 左右）。
        # x 相近时按基线从上到下排，保证 "PquerySPD(i,j)" 的阅读顺序。
        index = 0
        while index < len(group) - 1:
            end = index + 1
            while (
                end < len(group)
                and abs(group[end]["bbox"][0] - group[index]["bbox"][0]) <= 1.5
            ):
                end += 1
            if end - index > 1:
                group[index:end] = sorted(
                    group[index:end], key=lambda s: s["bbox"][1]
                )
            index = end
        if not group:
            continue
        # 同一 y 上可能并排着左右栏：按水平间隙切回各自的视觉行。
        # 上下标与基础符号的间隙都小于 2pt，不会被切开；双栏缝通常 > 20pt。
        runs: list[list[dict]] = []
        current: list[dict] = []
        prev_x1: float | None = None
        for span in group:
            x0, x1 = span["bbox"][0], span["bbox"][2]
            if (
                current
                and prev_x1 is not None
                and x0 - prev_x1 > max(12.0, base * 1.2)
            ):
                runs.append(current)
                current = []
                prev_x1 = None
            current.append(span)
            prev_x1 = x1 if prev_x1 is None else max(prev_x1, x1)
        if current:
            runs.append(current)
        for run in runs:
            out.append({
                "spans": run,
                "bbox": (
                    min(s["bbox"][0] for s in run),
                    min(s["bbox"][1] for s in run),
                    max(s["bbox"][2] for s in run),
                    max(s["bbox"][3] for s in run),
                ),
                "dir": (1, 0),
                "wmode": 0,
                "type": 0,
            })

    if leftovers:
        kept = {id(s) for s in leftovers}
        for line in raw_lines:
            line_spans = [s for s in line.get("spans") or [] if id(s) in kept]
            if not line_spans:
                continue
            out.append({
                **{k: v for k, v in line.items() if k != "spans"},
                "spans": line_spans,
            })
    out.sort(key=lambda line: (line["bbox"][1], line["bbox"][0]))
    return out


def _collect_lines(doc: pymupdf.Document) -> list[Line]:
    lines: list[Line] = []
    for pno in range(len(doc)):
        page = doc[pno]
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for raw in _merge_visual_lines(b.get("lines") or []):
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
        t = ln.text.strip()
        # 页码常落在 7.5% 页眉/页脚带边缘之外一点（如 59.9pt vs 59.4pt），
        # 几何阈值判定会漏掉，随后被并进正文段落。页边 12% 内的纯数字
        # 一律当页码处理。
        if re.fullmatch(r"\d{1,4}", t) and (
            ln.y1 < h * 0.12 or ln.y0 > h * 0.90
        ):
            ln.is_margin = True
            continue
        # Nature 等期刊的通栏图注/表题经常紧贴页面顶端（图注首行正好落在
        # 7.5% 页眉带内）。只看几何位置会把它当页眉丢掉，后面的图注续行
        # 变成正文、表格也因为没有表题而无法裁图。
        if ln.y1 < h * _HEADER_RATIO and not _CAPTION_RE.match(ln.text.strip()):
            ln.is_margin = True
        elif ln.y0 > h * _FOOTER_RATIO:
            ln.is_margin = True
    # 同一文本在多页重复出现（≥ 3 页且 ≥ 30% 篇幅）→ 页眉页脚
    if len(doc) >= 3:
        norm = Counter()
        for ln in lines:
            if ln.is_margin:
                continue
            t = ln.text.strip()
            # 图注/表注不能按「跨页重复」删掉：Agri-SAGE 这类论文每页都是
            # 「Fig. 1.」「Fig. 2.」，归一化数字后完全相同，会被误判成页眉。
            if _CAPTION_RE.match(t) and _is_caption_text(t):
                continue
            key = re.sub(r"\d+", "#", ln.text.strip().lower())
            if 3 <= len(key) <= 120:
                norm[key] += 1
        threshold = max(3, int(len(doc) * 0.3))
        repeats = {k for k, c in norm.items() if c >= threshold}
        # 各页非编号行的右边界，用于区分「贴在栏右缘的公式编号」与真正的页码
        edge_xs: dict[int, list[float]] = {}
        for ln in lines:
            if ln.is_margin or _EQ_NUM_ONLY_RE.match(ln.text.strip()):
                continue
            edge_xs.setdefault(ln.page, []).append(ln.x1)
        edge_xs = {p: sorted(v) for p, v in edge_xs.items()}
        for ln in lines:
            key = re.sub(r"\d+", "#", ln.text.strip().lower())
            if key in repeats and ln.page > 1:
                # 右边缘的「(2)」这类是公式编号，不是页码：每页公式编号会重复
                # 出现 (1)(2)...，归一化后都是 (##)，不能被当成页眉页脚丢掉。
                # 双栏左栏的编号贴在栏右缘、不到整页 60%，须按栏边界判断。
                if _EQ_NUM_ONLY_RE.match(ln.text.strip()):
                    w = widths.get(ln.page - 1, 612)
                    if ln.x1 > w * 0.6 or _aligns_column_edge(
                        edge_xs.get(ln.page, []), ln.x1
                    ):
                        continue
                ln.is_margin = True


# ── 栏检测 ─────────────────────────────────────────────────────────────

def _text_area(lines: list[Line]) -> tuple[float, float, float, float]:
    xs0 = [ln.x0 for ln in lines]
    xs1 = [ln.x1 for ln in lines]
    ys0 = [ln.y0 for ln in lines]
    ys1 = [ln.y1 for ln in lines]
    return min(xs0), min(ys0), max(xs1), max(ys1)


def _looks_like_data_row(text: str) -> bool:
    """公式编号 / 表格数据列不能作为「这里有一条栏」的证据。

    单栏论文的公式编号会贴着页面右缘、表格数据列会另起一条 x0，二者都
    能凑出「右栏」的假象，把单栏页误判成双栏。
    """
    t = text.strip()
    if not t:
        return True
    if _EQ_NUM_ONLY_RE.match(t):
        return True
    if len(t) <= 3 and not t.isalpha():
        return True
    digits = sum(1 for ch in t if ch.isdigit())
    return digits / len(t) > 0.7


_MATH_SYMBOL_RE = re.compile(r"[=+＋−±×÷θρβαδμσλ∑∫√≈≤≥]")


def _looks_like_math_fragment(ln: Line) -> bool:
    """公式/公式碎片行：不能作为「这里有一条栏」的证据。

    单栏论文的长公式会被拆成贴着右侧的几行碎片（`i ) and θv`、
    `k=1 exp(...)`），左边界雷同，很容易凑成假右栏。
    """
    if ln.math:
        return True
    t = ln.text.strip()
    if not t:
        return True
    if ln.inline_math:
        covered = sum(max(0, int(m["end"]) - int(m["start"])) for m in ln.inline_math)
        if covered / len(t) >= 0.45:
            return True
    symbols = _MATH_SYMBOL_RE.findall(t)
    # 两个以上数学符号：公式行/公式混排行，不是普通正文
    if len(symbols) >= 2:
        return True
    # 短行 + 数学符号密集才算碎片；正文里的「accuracy = 90%」不能排除。
    if len(t) <= 24 and symbols:
        if (len(symbols) + sum(ch.isdigit() for ch in t)) / len(t) >= 0.3:
            return True
    return False


def _caption_lines(lines: list[Line]) -> set[int]:
    """图注标题及其紧邻续行（同左边界、小行距）。

    单栏论文的通栏图注常常另起一条左边界并连续排成多行，看起来很像一条
    窄栏；栏检测必须把它们排除，否则单栏页会被误判成双栏。
    """
    skip: set[int] = set()
    ordered = sorted(lines, key=lambda l: (l.y0, l.x0))
    i = 0
    while i < len(ordered):
        if not _CAPTION_RE.match(ordered[i].text.strip()):
            i += 1
            continue
        skip.add(id(ordered[i]))
        j = i + 1
        while j < len(ordered):
            cur, prev = ordered[j], ordered[j - 1]
            gap = cur.y0 - prev.y1
            if abs(cur.x0 - prev.x0) > 14 or gap > 6 or gap < -2:
                break
            if cur.size > prev.size + 1.0:
                break
            skip.add(id(cur))
            j += 1
        i = j
    return skip


# 宽字距标题（"A B S T R A C T"、"K E Y W O R D S"）在 PDF 文本层里是一串
# 带空格的单字母 token，语义识别必须容忍这种排版，否则整段摘要会落回正文。
_HEAD_GAP = r"[\s\u00a0\u2000-\u200b\u00ad]*"


def _spaced_label(word: str) -> str:
    return _HEAD_GAP.join(word)


_ABSTRACT_HEAD_RE = re.compile(
    rf"^\s*(?:Abstract|ABSTRACT|{_spaced_label('ABSTRACT')}|摘{_HEAD_GAP}要)"
    r"(?![A-Za-z\u3400-\u9fff])"
)
_ABSTRACT_LABEL_RE = re.compile(
    rf"^\s*(?:{_spaced_label('ABSTRACT')}|{_spaced_label('摘要')})\s*[:：.—–-]?\s*$",
    re.IGNORECASE,
)
_KEYWORDS_HEAD_RE = re.compile(
    rf"^\s*(?:Keywords?|{_spaced_label('KEYWORDS')}|Index{_HEAD_GAP}Terms|"
    rf"{_spaced_label('INDEXTERMS')}|关{_HEAD_GAP}键{_HEAD_GAP}词)"
    r"(?![A-Za-z\u3400-\u9fff])",
    re.IGNORECASE,
)
_INTRO_HEAD_RE = re.compile(
    rf"^\s*(?:(?:\d+|[IVXLC]+)\.?\s+)?(?:Introduction|INTRODUCTION|"
    rf"{_spaced_label('INTRODUCTION')}|引{_HEAD_GAP}言|绪{_HEAD_GAP}论)"
    r"(?![A-Za-z\u3400-\u9fff])"
)


def _is_abstract_head(text: str) -> bool:
    return bool(_ABSTRACT_HEAD_RE.match(text) or _ABSTRACT_LABEL_RE.match(text))
# 作者姓名前的编号/符号：1st / 2nd / 3 / * / †。IEEE 用 1st/2nd，其它期刊用
# 上标数字或符号，统一剥掉后再判断姓名形态。
_AUTHOR_PREFIX_RE = re.compile(
    r"^\s*(?:\d{1,2}(?:st|nd|rd|th)|[*†‡§¶#]|\d+)\s*", re.IGNORECASE
)
# 国名/常见国家词不是作者姓名。地名不可能穷举，但配合「卡片首行 + 至少
# 两个词」的约束，足以挡住 "Changsha, China" 这类地址行。
_PLACE_RE = re.compile(
    r"\b(?:China|USA|U\.S\.A|UK|United\s+States|Japan|Germany|France|Canada|"
    r"Australia|India|Korea|Singapore|Italy|Spain|Netherlands|Brazil|Chile|"
    r"Mexico|Switzerland|Sweden|Denmark|Belgium|Austria|Israel|Taiwan|Russia|"
    r"Poland|Portugal|Ireland|New\s+Zealand|South\s+Africa|Argentina|Colombia|"
    r"Peru|Finland|Norway|Greece|Turkey|Czech|Hungary)\b",
    re.IGNORECASE,
)


@dataclass
class FrontMatter:
    """首页前置区（标题/作者/摘要/关键词）的版面范围。

    首页不是「单栏或双栏」二选一：常见版式是**通栏标题 + 三列作者卡片 +
    左栏摘要 + 右栏正文**同时存在。用正文的 gutter 去切前置区，会把中间
    那一列作者当成「跨栏锚点」，作者随之被排到摘要甚至 Introduction 之后。
    这里把前置区、摘要、关键词的 y/x 范围显式记下来，阅读顺序、建块与
    分类三处共用同一份判断，不再各自猜。
    """

    page: int = 0
    head_end: float = 0.0
    abstract: tuple[float, float] | None = None
    abstract_col: int = 0
    # 摘要/关键词所在的主列 x 范围。首页常见「左侧通讯/单位窄列 + 右侧摘要
    # 主列」；只按 y/col 判断会把左侧窄列的小字夹进摘要，摘要范围还会被
    # 异列邮箱行提前截断。x 范围是这两类版式的共同锚点。
    abstract_x: tuple[float, float] | None = None
    keywords: tuple[float, float] | None = None
    keywords_col: int = 0
    keywords_x: tuple[float, float] | None = None
    # 首页辅助窄列（通讯作者、邮箱、单位）。它与摘要同 y 但不属于正文流；
    # 单独记录后由阅读顺序排到摘要之前/之后，并由分类阶段标成 affiliation。
    aux: tuple[float, float] | None = None
    aux_x: tuple[float, float] | None = None

    def in_head(self, bbox: tuple[float, float, float, float]) -> bool:
        # 左右栏同一行的 y0 常有 0.5~1pt 的差（Agri-SAGE 右栏正文比左栏
        # Abstract 高 0.7pt）。留 2pt 容差，免把右栏正文首行算进作者区。
        return self.head_end > 0 and bbox[1] < self.head_end - 2.0

    @staticmethod
    def _x_matches(bbox: tuple[float, float, float, float], span: tuple[float, float]) -> bool:
        x0, x1 = span
        width = max(bbox[2] - bbox[0], 1.0)
        overlap = min(bbox[2], x1) - max(bbox[0], x0)
        return overlap >= 0.35 * width

    def in_abstract(self, ln: Line) -> bool:
        if self.abstract is None:
            return False
        y0, y1 = self.abstract
        if not (y0 - 0.5 <= ln.y0 < y1):
            return False
        if self.abstract_col >= 0 and ln.col not in (self.abstract_col, -1):
            return False
        if self.abstract_x is not None and not self._x_matches(ln.bbox, self.abstract_x):
            return False
        return True

    def is_abstract_head(self, ln: Line) -> bool:
        return (
            self.abstract is not None
            and abs(ln.y0 - self.abstract[0]) < 1.0
            and _is_abstract_head(ln.text)
        )

    def in_keywords(self, ln: Line) -> bool:
        if self.keywords is None:
            return False
        y0, y1 = self.keywords
        if not (y0 - 0.5 <= ln.y0 < y1):
            return False
        if self.keywords_col >= 0 and ln.col not in (self.keywords_col, -1):
            return False
        if self.keywords_x is not None and not self._x_matches(ln.bbox, self.keywords_x):
            return False
        return True

    def in_aux(self, bbox: tuple[float, float, float, float]) -> bool:
        if self.aux is None:
            return False
        y0, y1 = self.aux
        if not (y0 - 0.5 <= bbox[1] <= y1 + 0.5):
            return False
        return self.aux_x is None or self._x_matches(bbox, self.aux_x)


def _first_body_line_y(
    lines: list[Line], body_size: float, page_width: float
) -> float:
    """没有 Abstract/Introduction 标记时，用第一段正文判定前置区结束。

    只认「够长 + 正文号字号 + 写满一栏」的连续行，避免把作者卡片的学校、
    邮箱或图注当成正文起点。作者行（含一长串逗号分隔的姓名）必须排除，
    否则 Nature 这类「标题下方直接是摘要」的版式会把作者区划到正文里。
    """
    for ln in sorted(lines, key=lambda l: (l.y0, l.x0)):
        text = ln.text.strip()
        if len(text) < 70 or ln.width < page_width * 0.32:
            continue
        if body_size > 0 and not (body_size * 0.85 <= ln.size <= body_size * 1.35):
            continue
        if (
            _AFFIL_RE.search(text)
            or "@" in text
            or _CAPTION_RE.match(text)
            or _KEYWORDS_HEAD_RE.match(text)
            or _looks_author_name(text)
            or re.search(
                r"https?://|doi:|received:|accepted:|published|open\s+access",
                text,
                re.IGNORECASE,
            )
        ):
            continue
        # 正文是连续的：标题行、作者行、期刊栏目名常常也够长，但下面
        # 紧跟的不是同字号的正文行。要求后面 1~2 行里至少有一行同样是
        # 正文特征，避免把大字号标题当成正文起点。
        following = [
            x
            for x in sorted(lines, key=lambda l: (l.y0, l.x0))
            if x is not ln
            and x.page == ln.page
            and 0 < x.y0 - ln.y0 < max(3.0 * max(ln.height, 1.0), 40.0)
        ][:2]
        if not any(
            len(x.text.strip()) >= 50
            and (body_size <= 0 or body_size * 0.85 <= x.size <= body_size * 1.35)
            and not _AFFIL_RE.search(x.text)
            and "@" not in x.text
            and not _looks_author_name(x.text)
            for x in following
        ):
            continue
        return ln.y0
    return 0.0


def _body_flow_start(lines: list[Line], start_y: float, page_width: float) -> float:
    """在首页前置区之后找「正文流」的起点（双栏/半栏宽的文字行）。

    Nature 等版式的摘要常是通栏宽行，正文则是半栏宽的双栏。用「宽度
    明显小于页宽 + 同栏连续」找得到正文起点，就能把摘要和正文分开，
    而不会把后面的参考文献/表格说明一起吞进摘要。
    """
    half = page_width * 0.55
    candidates = sorted(
        [ln for ln in lines if ln.y0 > start_y + 1], key=lambda l: l.y0
    )
    for i, ln in enumerate(candidates):
        if ln.width >= half or ln.col not in (0, 1):
            continue
        following = [
            x
            for x in candidates[i + 1: i + 4]
            if x.col == ln.col and x.y0 - ln.y0 < 40
        ]
        if any(x.width < half for x in following):
            return ln.y0
    return 0.0


def _detect_front_matter(
    lines: list[Line], body_size: float, page_height: float, page_width: float
) -> FrontMatter:
    """识别首页前置区与摘要/关键词范围。

    摘要标题不一定在第 1 页：Elsevier 等版式第 1 页只放 Highlights/封面，
    第 2 页顶部才是标题、作者与 ABSTRACT。前置区页以「最早出现摘要标题的
    页」为准，找不到摘要标题时仍回退到第 1 页。
    """
    front = FrontMatter()
    visible = [ln for ln in lines if not ln.is_margin and ln.text.strip()]
    pages = sorted({ln.page for ln in visible})
    if not pages:
        return front
    front_page = pages[0]
    abs_ln = None
    for pno in pages[:3]:
        cand = next(
            (ln for ln in visible if ln.page == pno and _is_abstract_head(ln.text)),
            None,
        )
        if cand is not None:
            abs_ln = cand
            front_page = pno
            break
    page_lines = sorted(
        [ln for ln in visible if ln.page == front_page],
        key=lambda l: (l.y0, l.x0),
    )
    if len(page_lines) < 6:
        return front
    line_h = median([ln.height for ln in page_lines]) or max(body_size * 1.2, 1.0)
    top = page_lines[0].y0
    head_candidates = [ln for ln in page_lines if ln.y0 < page_height * 0.8]
    if abs_ln is None:
        abs_ln = next((ln for ln in head_candidates if _is_abstract_head(ln.text)), None)
    kw_ln = next((ln for ln in head_candidates if _KEYWORDS_HEAD_RE.match(ln.text)), None)
    intro_ln = next((ln for ln in head_candidates if _INTRO_HEAD_RE.match(ln.text)), None)
    if intro_ln is not None and intro_ln.y0 <= top + line_h:
        intro_ln = None

    def _same_flow(ln: Line, ref: Line, col: int) -> bool:
        """同栏且同一条文字流：首页左侧通讯/单位窄列与右侧摘要列同 col、
        同页，但 x 范围不重叠，不能沿 y 当作同一段继续延伸。"""
        if col >= 0 and ln.col not in (col, -1):
            return False
        overlap = min(ln.x1, ref.x1) - max(ln.x0, ref.x0)
        return overlap > min(ln.width, ref.width) * 0.25

    def _range_end(start: Line, col: int, stops: list[Line | None]) -> float:
        ends = [
            ln.y0
            for ln in stops
            if ln is not None and ln.y0 > start.y0 + 1 and _same_flow(ln, start, col)
        ]
        same_col = sorted(
            [
                ln
                for ln in page_lines
                if ln.y0 > start.y0 + 1 and _same_flow(ln, start, col)
            ],
            key=lambda l: l.y0,
        )
        prev = start
        for ln in same_col:
            # 同一摘要/关键词流里出现下一个粗体小标题（如 Background）就结束；
            # 它与摘要正文同 y 范围，但属于下一节，不是摘要续行。
            if (
                ln.bold
                and ln.size > start.size + 0.2
                and len(ln.text.strip()) <= 60
                and not ln.text.strip().lower().startswith(("keywords", "abstract"))
            ):
                ends.append(ln.y0)
                break
            gap = ln.y0 - prev.y1
            if gap > max(line_h * 1.9, 7.0) or abs(ln.size - start.size) > max(
                2.0, start.size * 0.25
            ):
                ends.append(ln.y0)
                break
            prev = ln
        ends.append(start.y0 + page_height)
        return min(ends)

    body_y = _first_body_line_y(page_lines, body_size, page_width)

    if abs_ln is not None:
        col = abs_ln.col if abs_ln.col in (0, 1) else -1
        end = _range_end(abs_ln, col, [kw_ln, intro_ln])
        front.page = front_page
        front.head_end = min(abs_ln.y0, body_y) if body_y > 0 else abs_ln.y0
        if end > abs_ln.y0 + 1:
            front.abstract = (abs_ln.y0, end)
            front.abstract_col = col
            abstract_lines = [
                ln for ln in page_lines
                if abs_ln.y0 <= ln.y0 < end and _same_flow(ln, abs_ln, col)
            ]
            if abstract_lines:
                front.abstract_x = (
                    abs_ln.x0,
                    max(ln.x1 for ln in abstract_lines),
                )
    elif intro_ln is not None:
        front.page = front_page
        front.head_end = min(intro_ln.y0, body_y) if body_y > 0 else intro_ln.y0
    elif kw_ln is not None:
        front.page = front_page
        front.head_end = min(kw_ln.y0, body_y) if body_y > 0 else kw_ln.y0
    elif body_y > top + line_h * 2 and body_y < page_height * 0.65:
        # 没有 Abstract/Introduction/Keywords 标记时，只切出「标题+作者区」，
        # 不自动把后面的第一段文字当摘要：有的论文首页直接是正文/参考文献，
        # 自动造 abstract 会把引用条目、表格说明一起吞掉。
        front.page = front_page
        front.head_end = body_y
        # Nature 类版式：标题下有作者区、摘要通栏、正文双栏。只有确认
        # 「有作者区 + 能找到半栏正文起点」才把中间这段标成摘要。
        author_zone = any(
            _looks_author_name(ln.text) or _AFFIL_RE.search(ln.text)
            for ln in page_lines
            if ln.y0 < body_y - 1
        )
        flow_y = _body_flow_start(page_lines, body_y, page_width)
        if author_zone and flow_y > body_y + line_h * 2:
            front.abstract = (body_y, flow_y)
            front.abstract_col = -1
            first = min(
                (ln for ln in page_lines if ln.y0 >= body_y - 0.5),
                key=lambda l: l.y0,
                default=None,
            )
            if first is not None:
                front.abstract_x = (first.x0, first.x1)

    if kw_ln is not None:
        col = kw_ln.col if kw_ln.col in (0, 1) else -1
        kw_end = _range_end(kw_ln, col, [intro_ln])
        front.keywords = (kw_ln.y0, kw_end)
        front.keywords_col = col
        kw_lines = [
            ln for ln in page_lines
            if kw_ln.y0 <= ln.y0 < kw_end and _same_flow(ln, kw_ln, col)
        ]
        if kw_lines:
            front.keywords_x = (kw_ln.x0, max(ln.x1 for ln in kw_lines))
        if front.page:
            front.head_end = min(front.head_end, kw_ln.y0)

    # 首页辅助窄列：通讯/邮箱/单位与摘要并列，不属于摘要或正文流。
    # 以「摘要主列的左缘」为界，收集其左侧的小字号机构/邮箱行。
    if front.page and abs_ln is not None:
        start_y = min(front.head_end, abs_ln.y0) - 4.0
        end_y = front.keywords[1] if front.keywords is not None else (
            front.abstract[1] if front.abstract is not None else abs_ln.y0 + page_height * 0.4
        )
        aux_lines = [
            ln for ln in page_lines
            if start_y <= ln.y0 <= end_y
            and ln.x1 <= abs_ln.x0 - 6
            and ln.size < abs_ln.size * 0.9
            and not front.in_abstract(ln)
            and not front.in_keywords(ln)
        ]
        # 单位最后一行（城市/国家）常不含机构关键词，不能只按文本种子；
        # 只要落在摘要主列左侧、字号明显小于摘要，就同属辅助窄列。
        if aux_lines:
            front.aux = (min(ln.y0 for ln in aux_lines), max(ln.y1 for ln in aux_lines))
            front.aux_x = (min(ln.x0 for ln in aux_lines), max(ln.x1 for ln in aux_lines))
    return front


def _detect_gutter(lines: list[Line]) -> float | None:
    """检测双栏之间的空白带，返回 gutter 中心 x；单栏返回 None。

    关键改动：先按行左边界聚类，只保留「贴着栏左边界」的行再做投影。
    旧的「所有窄行」投影会被居中作者行、跨栏图注填满 gutter（它们的
    左边界落在页面中部），使大量双栏页被判成单栏，正文左右栏随后被
    逐行交错拼进同一段。这里再做两步：投影找 ≥6pt 空白带；失败时用
    左右栏的实际边界回退（栏缝常被量化误差吃掉 1~2pt）。
    """
    body = [ln for ln in lines if not ln.is_margin and ln.text.strip()]
    if len(body) < 8:
        return None
    x0, _, x1, _ = _text_area(body)
    width = x1 - x0
    if width <= 0:
        return None

    raw_col = [ln for ln in body if ln.width < width * 0.62]
    skip_captions = _caption_lines(body)
    col_lines = [
        ln for ln in raw_col
        if (
            not _looks_like_math_fragment(ln)
            and id(ln) not in skip_captions
            and not _looks_like_data_row(ln.text)
        )
    ]
    # 公式/数据密集的论文会把过滤做得过头（正文行也被判成公式碎片），
    # 此时退回未过滤的窄行集合，保持原有双栏检测能力。
    if len(col_lines) < 0.4 * len(raw_col):
        col_lines = raw_col
    if len(col_lines) < 0.4 * len(body):
        return None  # 大部分是通栏宽行 → 单栏

    def main_start(lo: float, hi: float) -> float | None:
        """区间内最常见的行左边界（4pt 分箱），作为该栏的起点。"""
        bins: Counter[int] = Counter()
        for ln in col_lines:
            off = ln.x0 - x0
            if lo <= off <= hi:
                bins[int(off // 4) * 4] += 1
        if not bins:
            return None
        return bins.most_common(1)[0][0] + 2.0

    left_start = main_start(0.0, width * 0.30)
    right_start = main_start(width * 0.45, width)
    if left_start is None or right_start is None:
        return None
    if right_start - left_start < width * 0.22:
        return None  # 两个左边界太近，是同一栏的不同缩进

    left_rows = [ln for ln in col_lines if abs(ln.x0 - x0 - left_start) <= 8]
    right_rows = [ln for ln in col_lines if abs(ln.x0 - x0 - right_start) <= 8]
    # 右栏可能只有半页（另一侧被大图/表格占住），用 15% 的同一阈值会把
    # 这种页判成单栏，左右栏正文随后按 y 交错拼进同一段。左栏仍要求足量，
    # 右栏只要形成一簇（≥4 行）即可；零星居中标题不足以凑够 4 行。
    need_left = max(5, int(len(col_lines) * 0.15))
    need_right = max(4, int(len(col_lines) * 0.05))
    if len(left_rows) < need_left or len(right_rows) < need_right:
        return None
    # 单栏页里成段的多行图注（左边界固定、宽度窄）也会凑出一条「右栏」，
    # 但只占页面上一小块。真双栏的右栏文字即使被图占去大半，也会覆盖
    # 页面正文高度的至少一成（目标论文第 4 页右栏只有顶部 6 行正文）。
    body_span = max(ln.y1 for ln in body) - min(ln.y0 for ln in body)
    right_span = max(ln.y1 for ln in right_rows) - min(ln.y0 for ln in right_rows)
    if body_span <= 0 or right_span < 0.10 * body_span:
        return None
    # 栏正文是成行的连续文字；公式碎片/页码/零散标注都比半栏窄得多。
    right_widths = sorted(ln.width for ln in right_rows)
    if right_widths[len(right_widths) // 2] < width * 0.18:
        return None

    bins = int(width) + 1
    cover = [0] * bins
    for ln in left_rows + right_rows:
        a = max(0, min(bins - 1, int(ln.x0 - x0)))
        b = max(0, min(bins - 1, int(ln.x1 - x0)))
        for i in range(a, b + 1):
            cover[i] += 1

    lo, hi = int(bins * 0.30), int(bins * 0.72)
    peak = max(cover) if cover else 0
    tol = max(1, int(peak * 0.02))
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
    if gap_w >= 6 and s is not None:
        # 两侧都必须有足够多的栏行，否则只是栏内的局部空隙
        left = sum(1 for ln in left_rows if ln.x1 <= x0 + s + 2)
        right = sum(1 for ln in right_rows if ln.x0 >= x0 + e - 2)
        if left >= max(4, len(left_rows) * 0.5) and right >= max(4, len(right_rows) * 0.5):
            return x0 + (s + e) / 2

    # 投影被少量跨栏文字打断时，退回左右栏的实际边界
    l_edge = sorted(ln.x1 for ln in left_rows)[int(len(left_rows) * 0.9)]
    r_edge = sorted(ln.x0 for ln in right_rows)[int(len(right_rows) * 0.1)]
    gutter = (l_edge + r_edge) / 2
    if r_edge - l_edge >= 5.5 and x0 + width * 0.30 <= gutter <= x0 + width * 0.78:
        return gutter
    return None


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


def _split_row_runs(row: list[Line], gutter: float | None) -> list[list[Line]]:
    """把同一基线上的碎片按栏缝或异常间距切成若干组。

    双栏页：分居 gutter 两侧的碎片属于左右栏，拼在一起就是交错串行。
    单栏页：间距明显超过正常词距（表格对齐、目录页码）时也断开，避免
    把不相邻的两段文字粘成一句。
    """
    if not row:
        return []
    runs: list[list[Line]] = [[row[0]]]
    prev_x1 = row[0].x1
    for m in row[1:]:
        gap = m.x0 - prev_x1
        split = False
        if gutter is not None and prev_x1 <= gutter + 1 and m.x0 >= gutter - 1:
            split = True
        elif (
            gap > max(18.0, 2.4 * max(m.size, 1.0))
            and len(m.text.strip()) >= 6
            and len(runs[-1][-1].text.strip()) >= 6
        ):
            split = True
        if split:
            runs.append([m])
            prev_x1 = m.x1
        else:
            runs[-1].append(m)
            prev_x1 = max(prev_x1, m.x1)
    return runs


def _run_column(run: list[Line], col: int, gutter: float | None) -> int:
    """跨栏行被按栏缝拆开后，把每一段归回它所在的栏。"""
    if gutter is None or col != -1:
        return col
    if max(m.x1 for m in run) <= gutter + 1:
        return 0
    if min(m.x0 for m in run) >= gutter - 1:
        return 1
    return -1


# 公式碎片之间允许跨过的「正体词」只应是公式里的 front/down/query 这类
# 术语；and/the/of/for 是正文词，跨过去会把正文裁进公式小图。
_MATH_BRIDGE_STOPWORDS = {
    "and", "the", "of", "for", "with", "to", "in", "on", "as", "is", "are",
    "be", "by", "from", "we", "it", "its", "this", "that", "these", "those",
    "when", "where", "while", "which", "whose", "than", "then", "also",
    "such", "using", "used", "can", "may", "not", "all", "any", "each",
    "per", "via", "if", "or", "at", "into", "over", "under",
}


def _math_bridge_latex(bridge: str) -> str | None:
    """两个公式片段之间的桥接文本 → LaTeX；无法安全表达时返回 None。

    合并碎片时不能一律降级裁图：两侧都已成功重建时，桥接的逗号/括号/
    运算符（以及 front/down 这类正体词）完全可以拼回同一段 LaTeX。
    """
    text = bridge.strip()
    if not text:
        return ""
    if re.fullmatch(r"[A-Za-z]{2,12}", text):
        return r"\text{" + text + "}"
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        if ch in SYMBOLS:
            out.append(SYMBOLS[ch])
        elif ch.isascii() and (
            ch.isalnum() or ch in "()[]{},.=+*/-<>|:!'"
        ):
            out.append(ch)
        else:
            return None
    return "".join(out)


def _merge_math_fragments(text: str, inline_math: list[dict]) -> list[dict]:
    """把视觉行合并后相邻的公式碎片拼回一段。

    inline_math 是在单条 PyMuPDF line 内分组的；一条公式被拆到多条 line
    （上下标、正体 front/down、CMEX 大括号片段）时，合并后仍是若干首尾
    相接的碎片。这里按字符区间再合并一次并统一走裁图（fallback），避免
    前端显示 `x_t ∈ xfront t` 这种残缺 latex。
    """
    if len(inline_math) < 2:
        return inline_math
    items = sorted(inline_math, key=lambda m: (int(m["start"]), int(m["end"])))
    merged: list[dict] = []
    for item in items:
        cur = {**item}
        if merged:
            prev = merged[-1]
            # 异常符号兜底按精确区间裁图，不能跨过中间文本合并；
            # 否则一张小图会覆盖到旁边的正文词。
            if prev.get("symbol") or cur.get("symbol"):
                merged.append(cur)
                continue
            gap = int(cur["start"]) - int(prev["end"])
            middle = text[int(prev["end"]): int(cur["start"])]
            bridge = middle.strip()
            # 只跨过空白、公式符号或公式里的正体短词（front / down）
            if -1 <= gap <= 12 and (
                not bridge
                or (
                    bridge.isalpha()
                    and len(bridge) <= 12
                    and bridge.lower() not in _MATH_BRIDGE_STOPWORDS
                )
                or not re.search(r"[A-Za-z]{2,}", bridge)
            ):
                bbox = prev.get("bbox") or cur.get("bbox")
                if prev.get("bbox") and cur.get("bbox"):
                    a, b = prev["bbox"], cur["bbox"]
                    bbox = [
                        min(a[0], b[0]), min(a[1], b[1]),
                        max(a[2], b[2]), max(a[3], b[3]),
                    ]
                prev["start"] = min(int(prev["start"]), int(cur["start"]))
                prev["end"] = max(int(prev["end"]), int(cur["end"]))
                # 两侧都已重建成功时，桥接的逗号/括号/运算符可以拼回同一段
                # LaTeX；只有一侧不可重建或拼接后括号不平衡才降级裁图。
                merged_tex = None
                if (
                    (prev.get("latex") or "")
                    and not prev.get("fallback")
                    and (cur.get("latex") or "")
                    and not cur.get("fallback")
                ):
                    bridge_tex = _math_bridge_latex(middle)
                    if bridge_tex is not None:
                        candidate = prev["latex"] + bridge_tex + cur["latex"]
                        if tex_braces_balanced(candidate):
                            merged_tex = candidate
                if merged_tex:
                    prev["latex"] = merged_tex
                    prev["fallback"] = False
                else:
                    prev["latex"] = ""
                    prev["fallback"] = True
                prev["source"] = text[int(prev["start"]): int(prev["end"])]
                if bbox:
                    prev["bbox"] = bbox
                continue
        merged.append(cur)
    return merged


def _union_rect(
    a: list[float] | tuple[float, ...] | None,
    b: list[float] | tuple[float, ...] | None,
):
    if not a:
        return list(b) if b else None
    if not b:
        return list(a)
    return [
        min(a[0], b[0]), min(a[1], b[1]),
        max(a[2], b[2]), max(a[3], b[3]),
    ]


def _inline_math_source(text: str, item: dict) -> str:
    value = (item.get("source") or "").strip()
    if value:
        return value
    start = int(item.get("start", 0))
    end = int(item.get("end", 0))
    if 0 <= start < end <= len(text):
        return text[start:end]
    return ""


def _join_math_latex(prev: dict, cur: dict, bridge: str) -> str | None:
    a = prev.get("latex") or ""
    b = cur.get("latex") or ""
    if not a or not b or prev.get("fallback") or cur.get("fallback"):
        return None
    middle = _math_bridge_latex(bridge)
    if middle is None:
        return None
    return a + middle + b


def _demote_unbalanced_math(text: str, inline_math: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in inline_math:
        source = _inline_math_source(text, item)
        if source and not tex_braces_balanced(source):
            item = {**item, "latex": "", "fallback": True}
        out.append(item)
    return out


def _merge_cross_row_math(text: str, inline_math: list[dict]) -> list[dict]:
    r"""把被换行切断的行内公式拼回一条。

    `build_inline_math` 以 PyMuPDF 的单条 line 为单位重建，集合公式
    `{2×` + `10^{-3},…}` 跨行时会得到两条各自花括号残缺的片段。这里只在
    「源文本净开 → 净闭」且中间没有正文时合并；拼不成完整公式的片段降级
    为裁图（fallback），避免前端拿到半截 LaTeX 后渲染成红字。
    """
    if not inline_math:
        return []
    items = sorted(inline_math, key=lambda m: (int(m["start"]), int(m["end"])))
    merged: list[dict] = []
    index = 0
    while index < len(items):
        cur = {**items[index]}
        acc_source = _inline_math_source(text, cur)
        if tex_brace_delta(acc_source) <= 0:
            merged.append(cur)
            index += 1
            continue
        cursor = index + 1
        while cursor < len(items):
            prev = items[cursor - 1]
            nxt = items[cursor]
            gap = int(nxt["start"]) - int(prev["end"])
            middle = text[int(prev["end"]): int(nxt["start"])] if gap > 0 else ""
            if gap < 0 or gap > 4 or middle.strip():
                break
            candidate = _join_math_latex(cur, nxt, middle)
            if candidate is None:
                break
            next_source = acc_source + _inline_math_source(text, nxt)
            cur = {
                **cur,
                "end": max(int(cur["end"]), int(nxt["end"])),
                "latex": candidate,
                "fallback": False,
                "source": next_source,
                "bbox": _union_rect(cur.get("bbox"), nxt.get("bbox")),
            }
            acc_source = next_source
            cursor += 1
            if tex_brace_delta(acc_source) == 0:
                break
        if cursor > index + 1 and tex_brace_delta(acc_source) == 0:
            merged.append(cur)
            index = cursor
        else:
            merged.append({**items[index]})
            index += 1
    return _demote_unbalanced_math(text, merged)


def _normalize_block_inline_math(blocks: list[Block]) -> list[Block]:
    for block in blocks:
        if block.inline_math:
            block.inline_math = _merge_cross_row_math(block.text, block.inline_math)
    return blocks


def _merge_visual_rows(
    lines: list[Line], gutters: dict[int, float | None] | None = None
) -> list[Line]:
    """把同一基线上的文本碎片合并成一条「视觉行」。

    LaTeX 会把行内公式（如 `Ã := D^{-1/2}`）画成独立的 glyph run，PyMuPDF 因此
    把它们识别成「同一基线上的另一条 line」。不合并的话，一句
    `Where Ã := ... denotes the normalized adjacency matrix.` 会被切成三块，
    翻译出来就是断句。合并后仍是 Line 对象，下游逻辑无需改动。

    `gutters` 是该页栏缝位置：跨缝的碎片不合并，防止左右栏被交错拼接。
    """
    groups: dict[tuple[int, int], list[Line]] = {}
    for ln in lines:
        if ln.is_margin:
            continue
        groups.setdefault((ln.page, ln.col), []).append(ln)

    rows: list[Line] = []
    for (page, col), group in groups.items():
        group.sort(key=lambda l: l.y0)
        gutter = (gutters or {}).get(page)
        # 按「文字基线」聚类：上标/下标、高公式都不会把下一行误并进来。
        # 容差要与 cluster 里已有一行比较（不能只比首行）：7pt 上标先建簇后，
        # 10pt 主行会被 7pt 的容差挡在外面，公式行被拆成多段、上下标还会
        # 单独变成一个 equation 块。
        pieces = [ln for ln in group if ln.brace_piece]
        normal = [ln for ln in group if not ln.brace_piece]
        clusters: list[list[Line]] = []
        for ln in normal:
            best: tuple[float, list[Line]] | None = None
            for c in clusters:
                tol = 0.45 * max(
                    ln.size or 10.0,
                    max((m.size or 10.0) for m in c),
                )
                diff = min(abs(ln.baseline - m.baseline) for m in c)
                if diff <= tol and (best is None or diff < best[0]):
                    best = (diff, c)
            if best is not None:
                best[1].append(ln)
            else:
                clusters.append([ln])

        # CMEX 大括号延伸片段 bbox 跨两行，baseline 常落在上一行正文；按
        # bbox 垂直重叠归到公式行，避免 `{`/`}` 被并进正文。
        for ln in pieces:
            best_key: float | None = None
            best_cluster: list[Line] | None = None
            for c in clusters:
                top = min(m.bbox[1] for m in c)
                bottom = max(m.bbox[3] for m in c)
                overlap = min(bottom, ln.bbox[3]) - max(top, ln.bbox[1])
                center_gap = abs(
                    (ln.bbox[1] + ln.bbox[3]) / 2 - (top + bottom) / 2
                )
                key = overlap if overlap > 0 else -center_gap
                if best_key is None or key > best_key:
                    best_key, best_cluster = key, c
            if best_cluster is not None and best_key is not None and best_key > -8:
                best_cluster.append(ln)
            else:
                clusters.append([ln])

        for c in clusters:
            c.sort(key=lambda l: l.x0)      # 同一行内按 x 从左到右拼
            for run in _split_row_runs(c, gutter):
                text = ""
                inline_math: list[dict] = []
                for m in run:
                    lead = len(m.text) - len(m.text.lstrip())
                    text, offset = _append_line_text(text, m.text)
                    for item in m.inline_math:
                        start_pos = int(item["start"]) - lead + offset
                        end_pos = int(item["end"]) - lead + offset
                        if start_pos < 0 or end_pos <= start_pos:
                            continue
                        inline_math.append(
                            {**item, "start": start_pos, "end": end_pos}
                        )

                total = sum(m.total_chars for m in run) or 1
                math_chars = sum(m.math_chars for m in run)
                main = max(run, key=lambda m: m.total_chars)
                value = text.strip()
                shift = len(text) - len(text.lstrip())
                inline_math = [
                    {**item, "start": item["start"] - shift, "end": item["end"] - shift}
                    for item in inline_math
                    if item["start"] >= shift and item["end"] - shift <= len(value)
                ]
                inline_math = _merge_math_fragments(value, inline_math)
                rows.append(
                    Line(
                        text=value,
                        bbox=(
                            min(m.bbox[0] for m in run), min(m.bbox[1] for m in run),
                            max(m.bbox[2] for m in run), max(m.bbox[3] for m in run),
                        ),
                        page=page,
                        col=_run_column(run, col, gutter),
                        size=main.size,
                        bold=sum(1 for m in run if m.bold) * 2 > len(run),
                        math=math_chars / total > 0.6,
                        math_chars=math_chars,
                        total_chars=total,
                        baseline=main.baseline,
                        inline_math=inline_math,

                    )
                )
    return [r for r in rows if r.text.strip()]


def _band_major(lines: list[Line]) -> list[Line]:
    """按「视觉带」输出：同一基线的碎片整组按 x 排序，再进入下一带。

    不能只按 (y0, x0) 排序：同一行的左右栏、三列作者卡片的 y0 常有
    0.0x pt 的量化差，严格按 y0 会把中间列排到左右列前面。
    """
    if not lines:
        return []
    ordered = sorted(lines, key=lambda l: ((l.baseline or l.y0), l.x0))
    bands: list[tuple[float, list[Line]]] = []
    for ln in ordered:
        base = ln.baseline or ln.y0
        tol = max(2.0, 0.45 * max(ln.size, 1.0))
        if bands and abs(base - bands[-1][0]) <= tol:
            bands[-1][1].append(ln)
        else:
            bands.append((base, [ln]))
    out: list[Line] = []
    for _, band in bands:
        band.sort(key=lambda l: (l.x0, l.y0))
        out.extend(band)
    return out


def _reading_order(
    lines: list[Line],
    gutter: float | None,
    front: FrontMatter | None = None,
    page_width: float = 0.0,
) -> list[Line]:
    """按阅读顺序返回行序列。

    首页前置区（标题/多列作者卡片/单位）不适用正文的双栏模型：
    它内部按「视觉带」逐带、带内按 x 排序，这样三列作者是 1→2→3
    而不是中列先出；进入摘要/正文之后才按栏排序。
    """
    body = [ln for ln in lines if not ln.is_margin]
    if gutter is None:
        # 单栏正文仍按 y 顺序；但首页可能有「左侧通讯/单位窄列 + 右侧摘要
        # 主列」的并列版式，直接按 (y,x) 排序会让两列逐行交错。先把首页
        # 的 head / aux / abstract / keywords 分别成流，再接其余正文。
        if front is not None and front.page > 0:
            fp = front.page
            own = [ln for ln in body if ln.page == fp]
            before = sorted(
                [ln for ln in body if ln.page < fp],
                key=lambda ln: (ln.page, ln.y0, ln.x0),
            )
            after = sorted(
                [ln for ln in body if ln.page > fp],
                key=lambda ln: (ln.page, ln.y0, ln.x0),
            )
            head = [ln for ln in own if front.in_head(ln.bbox)]
            aux = [ln for ln in own if front.in_aux(ln.bbox)]
            abs_ = [ln for ln in own if front.in_abstract(ln)]
            kws = [ln for ln in own if front.in_keywords(ln)]
            used = {id(ln) for ln in [*head, *aux, *abs_, *kws]}
            rest = [ln for ln in own if id(ln) not in used]
            return (
                before
                + _band_major(head)
                + _band_major(aux)
                + _band_major(abs_)
                + _band_major(kws)
                + sorted(rest, key=lambda ln: (ln.y0, ln.x0))
                + after
            )
        return sorted(body, key=lambda ln: (ln.page, ln.y0, ln.x0))

    ordered: list[Line] = []
    pages = sorted({ln.page for ln in body})
    for page in pages:
        plines = [ln for ln in body if ln.page == page]
        head: list[Line] = []
        aux: list[Line] = []
        if front is not None and front.page == page and front.head_end > 0:
            head = [ln for ln in plines if front.in_head(ln.bbox)]
            aux = [ln for ln in plines if front.in_aux(ln.bbox)]
            plines = [
                ln for ln in plines
                if not front.in_head(ln.bbox) and not front.in_aux(ln.bbox)
            ]
        ordered.extend(_band_major(head))
        ordered.extend(_band_major(aux))

        # 摘要/关键词是独立的一段阅读流：通栏摘要的某些行比页宽 55% 窄，
        # 若交给下面的锚点/归栏逻辑，摘要末行会被当成右栏文字排到正文后面。
        # 先按 y 逐行输出摘要区，再从正文里移除这些行。
        if front is not None and front.page == page:
            abs_lines = [ln for ln in plines if front.in_abstract(ln)]
            kw_lines = [ln for ln in plines if front.in_keywords(ln)]
            front_lines = abs_lines + kw_lines
            if front_lines:
                ordered.extend(_band_major(abs_lines))
                ordered.extend(_band_major(kw_lines))
                front_ids = {id(ln) for ln in front_lines}
                plines = [ln for ln in plines if id(ln) not in front_ids]

        # 只有真正横跨两栏的行才是「通栏锚点」。作者卡片中列这类只压住
        # gutter 的短行在前置区已经按视觉带排好，不会走到这里；正文里的
        # 居中短行保持 col=-1，交给下面的锚点逻辑，避免打乱图注/表格。

        spanning = sorted(
            [ln for ln in plines if ln.col == -1],
            key=lambda l: (l.baseline or l.y0),
        )
        col_lines = [ln for ln in plines if ln.col in (0, 1)]

        # 以跨栏元素为锚点切分 y 区间：锚点之上按 左栏→右栏 输出，再输出锚点本身。
        # 区间上限带一个基线容差，让与锚点同基线的左右栏文字归入锚点之前，
        # 不会因 0.0x pt 的 y0 差被切到锚点之后。
        prev_base = float("-inf")
        for sp in spanning:
            sp_base = sp.baseline or sp.y0
            tol = max(1.0, 0.3 * max(sp.size, 1.0))
            region = [
                ln
                for ln in col_lines
                if prev_base <= (ln.baseline or ln.y0) <= sp_base + tol
            ]
            ordered.extend(_col_major(region))
            ordered.append(sp)
            prev_base = sp_base + tol
        tail = [ln for ln in col_lines if (ln.baseline or ln.y0) > prev_base]
        ordered.extend(_col_major(tail))
    return ordered


def _col_major(lines: list[Line]) -> list[Line]:
    """同区内：先左栏（按视觉带），再右栏（按视觉带）。"""
    left = [ln for ln in lines if ln.col == 0]
    right = [ln for ln in lines if ln.col == 1]
    return _band_major(left) + _band_major(right)


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
# 中文没有大小写：汉字开头同样是「上一行没说完」的接续证据。
_CONTINUE_START = re.compile(r"^(?:[a-z]|\d|\(|\[|[\u3400-\u9fff])")


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


def _caption_continuation(prev: str, nxt: str) -> bool:
    """图注/表注的续行判断。

    续行通常是「同一句标题」的下一小段，长度有限；紧跟图注的完整长句
    是正文段落，不能并进 caption（否则正文被吞掉、也不再参与翻译）。
    """
    n = nxt.strip()
    if not n:
        return False
    # 下一行又是「图 3 / 表 2」开头，说明这是正文里的另一处引用或另一个
    # 图注，不是当前图注的续行。
    if _CAPTION_RE.match(n):
        return False
    if len(n) > 160:
        return False
    if _CJK_RE.search(n) and len(n) > 60:
        return False
    return _continues_paragraph(prev, nxt)


def _append_line_text(prev: str, nxt: str) -> tuple[str, int]:
    """合并两行，并返回 `nxt` 在新文本中的起始位置（行内公式偏移用）。"""
    prev = prev.rstrip()
    nxt = nxt.lstrip()
    if not prev:
        return nxt, 0
    if not nxt:
        return prev, len(prev)
    if _HYPHEN_END.search(prev) and nxt[:1].islower():
        return prev[:-1] + nxt, len(prev) - 1   # inter-\nnational → international
    if prev[-1:] in "-‐‑" and nxt[:1].isupper():
        return prev + nxt, len(prev)            # 保留连字符（如 3D-CNN）
    if not keep_space_between(prev, nxt):
        return prev + nxt, len(prev)            # 中文换行不补空格
    return prev + " " + nxt, len(prev) + 1


def _join_text(prev: str, nxt: str) -> str:
    """合并两行：处理 LaTeX 断词与空格。"""
    return _append_line_text(prev, nxt)[0]


def _is_heading(ln: Line, body_size: float) -> int:
    """判断行是否为标题，返回层级（0 表示不是标题）。"""
    text = ln.text.strip()
    if not text or len(text) > 120:
        return 0
    # 以冒号/分号收尾的是图注、说明或正文片段，不是章节标题。
    if text.endswith((":", ";")):
        return 0
    # 图内的方位/仪器短标签（N / E / MIRI）不能当章节标题。
    if len(text) <= 5 and text.isupper() and not text.endswith("."):
        return 0
    # 单位、地名、参考文献条目即使以编号/年份开头也不是章节标题。
    if _AFFIL_RE.search(text) or _PLACE_RE.search(text) or _looks_reference_entry(text):
        return 0
    if re.match(r"^(?:19|20)\d{2}[a-z]?\.\s", text):
        return 0
    if re.search(
        r"\b(?:Conference|Proceedings|Workshop|Symposium|Journal|arXiv|et\s+al)\b",
        text,
        re.IGNORECASE,
    ):
        return 0
    # AASTeX / ApJ 的章节标题常用正文字号 + 正体（CMR10）排版，只靠字号和
    # 粗体会全部漏掉。编号标题的形态很稳定：`1. INTRODUCTION`、
    # `2. DATA`、`2.1. NIRCam Observations & Data Reduction`。
    # 用「编号 + 短标题 + 全大写或 Title Case」识别，层级由编号段数决定。
    # 字母编号只接受 `A. CONTINUUM...` 这类全大写附录标题，避免把
    # `T. A. Thompson et al.` 这种正文引用行误判成标题。
    numbered = re.match(
        r"^\s*(?P<num>\d+(?:\.\d+)*\.?|[A-Z]\.(?=\s+[A-Z]{2,}\b))\s+(?P<name>\S.*)$",
        text,
    )
    if numbered:
        name = numbered.group("name").strip()
        raw_num = numbered.group("num")
        # 字母编号（A. CONTINUUM...）通常位于 APPENDIX 之下，层级按二级处理；
        # 数字编号按段数（1.→1，2.1.→2）。
        depth = 2 if raw_num[:1].isalpha() else raw_num.rstrip(".").count(".") + 1
        words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", name)
        title_case = bool(words) and all(
            w[:1].isupper()
            or w.lower() in {"of", "and", "the", "for", "in", "on", "to", "with", "a", "an"}
            for w in words
        )
        # 贡献列表常写成 `1. We propose...` / `2. We show...`：编号相同，
        # 但标题名很长、含句号空格或冒号。标题名应短而完整。
        title_shaped = (
            name
            and len(name) <= 80
            and len(name.split()) <= 12
            and not name.endswith((".", "?", ";", ",", ":"))
            and not re.match(r"^[\d(]", name)
            and ": " not in name
            and ". " not in name
        )
        if title_shaped:
            if name.isupper() or (depth >= 2) or len(name) <= 45:
                if name.isupper() or title_case:
                    return min(depth, 3)
    if ln.size >= body_size + 3.5:
        if not text.endswith((":", ";")) and ": " not in text and ". " not in text:
            return 1
    if ln.size >= body_size + 1.2:
        if not text.endswith((":", ";")) and ": " not in text and ". " not in text:
            return 2
    # 非编号标题里出现多个功能词（the/of/can/be/to...）通常是正文句子，
    # 只是字号或粗体被误判成标题。
    if numbered is None and len(_STOP_RE.findall(text)) >= 3:
        return 0
    if (
        _DECLARATION_HEAD_RE.fullmatch(text.rstrip(" :：").strip())
        and len(text) <= 60
        and text[:1].isupper()
    ):
        return 3
    m = _SECTION_RE.match(text)
    # 只认首字母大写的章节名：「methods take the random walk ...」是正文，不是标题
    if m and m.group("head")[:1].isupper() and len(m.group("head")) <= 40 and len(text) <= 60:
        return 1 if ln.size >= body_size else 2
    if (
        ln.bold
        and len(text) <= 60
        and not text.endswith((".", "?", ";", ",", ")", ":"))
        and text[:1].isupper()
        and ": " not in text
        and ". " not in text
        # 单字章节标题（Simulations / Funding / Declarations）是合法的；
        # 图内重复标签（Task Task）通常字号明显小于正文，用字号下限挡住。
        and (body_size <= 0 or ln.size >= body_size * 0.9)
        and len(re.findall(r"[A-Za-z]{2,}", text)) >= 1
    ):
        return 3
    if _NUM_HEADING_RE.match(text) and ln.bold:
        return 3
    # 中文编号小标题：编号与标题紧邻、整行较短，且不能是「2.2 倍」这类
    # 数字+单位短语（排除常见量词开头）。
    num = _NUM_HEADING_ZH_RE.match(text)
    if num and len(text) <= 40:
        after_num = text[num.end():].lstrip()
        if (
            after_num
            and _CJK_RE.match(after_num[:1])
            and not _ZH_MEASURE_RE.match(after_num)
        ):
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
# 文献引用是正文行内公式最常见的「伴生文本」：年份 / et al. / DOI。
_CITE_RE = re.compile(r"\bet\s+al\b|\b(?:19|20)\d{2}\b|doi:|arxiv", re.IGNORECASE)
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


def _looks_equation(
    text: str, is_math_font: bool, inline_math: list[dict] | None = None
) -> bool:
    """公式行：数学字体占比高，或几乎没有自然语言词。

    注意不能用「词少就算公式」这种宽松规则——`Where Ã := … denotes the normalized
    adjacency matrix.` 这类含行内公式的正文句子只有 4 个英文词，会被误判成公式，
    进而被并进上面的显示公式里、整段不翻译。
    """
    t = text.strip()
    # 以中文句末标点收尾、且含汉字的行是正文（`图 4(a)～(b) 所示。`），
    # 行内公式占比再高也不能当公式裁图。
    if _CJK_RE.search(t) and t.endswith(("。", "！", "？", "；")):
        return False
    # 正文里的行内公式常与文献引用 / 行尾句读同行：
    # `massive (M⋆ ∼ 4 × 10^10 M⊙; A. K. Leroy et al. 2021),`
    # 这类行是正文，只是句子里带公式，不能整行裁成公式图。
    if _CITE_RE.search(t):
        return False
    if re.search(r"\b(?:Fig|Figs|Eq|Eqs|Sec|Ref|Refs|Dec)\b\.?", t, re.IGNORECASE):
        return False
    if (
        len(t) >= 40
        and re.search(r"\b[a-z]{3,}\s+[a-z]{3,}\b", t)
        and t.rstrip().endswith((",", ".", ";", ":"))
    ):
        return False
    if len(t) >= 24 and t.rstrip().endswith("-") and re.search(r"[A-Za-z]{2,}", t):
        return False
    # `ratio (3.35 µm - ...) ≈ 0.77.` 这类「英文词开头 + 行尾句读」的行
    # 是正文句子里带公式；显示公式不会以自然语言词开头再以句号收尾。
    if re.match(r"^[a-z]{3,}\b", t) and t.rstrip().endswith((",", ".", ";", ":")):
        return False
    # 正文末行常只剩「数值范围 + 单位 + 句号」（t_cc ∼ 0.05-0.35 Myr.）。
    # 它数学字体占比高，但仍属于上一段正文；显示公式不会以句号收尾。
    if (
        len(t) <= 32
        and re.match(r"^[\d.−\-–—]+", t)
        and t.rstrip().endswith(".")
        and re.search(r"\b(?:Myr|Gyr|yr|kpc|Mpc|pc|km|mJy|erg|Hz|K)\b", t)
    ):
        return False
    if inline_math:
        covered = sum(max(0, int(m["end"]) - int(m["start"])) for m in inline_math)
        if text and covered / len(text) >= 0.55:
            # 行内公式占比高 ≠ 整行都是公式。AASTeX / 部分 arXiv 论文的
            # 正文字体就是 CMR10，若把 CMR 当公式字体，整篇正文都会被
            # 重建为 inline_math 并吞成 equation。这里用自然语言证据否决：
            # 含停用词或 ≥5 个英文词的行仍是正文，只是句中带公式。
            words = _WORD_RE.findall(t)
            if _STOP_RE.search(t) or len(words) >= 5:
                return False
            return True
    if is_math_font:
        # 数学字体占主导也可能是行文（"where q_i = ..."），先排除
        if _PROSE_LEAD_RE.match(text.strip()):
            return False
        return True
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


_EQ_NUMBER_RE = re.compile(
    r"(?:^|(?<=\s))[（(]\s*([A-Z]?\d{1,3}(?:\.\d+)?)\s*[)）]\s*[.,;]?\s*$"
)
# 夹在公式正文中间的编号：视觉行按 x 拼接时，编号可能落在上下标之间
# （如 `aij = ... √ (9) j + bij d`）。前后必须是空白/边界，避免误删
# `f(9)` 这类真正的数学内容。
_EQ_NUMBER_INLINE_RE = re.compile(
    r"(?:^|(?<=\s))[（(]\s*([A-Z]?\d{1,3}(?:\.\d+)?)\s*[)）](?=\s|$)"
)


def _split_eq_number(text: str) -> tuple[str, str]:
    """把公式里的右对齐编号 `(3)` 从公式正文里拆出来。

    编号要单独渲染（前端右对齐），不能留在 LaTeX 里：视觉模型会把
    `(3)` 当成公式的一部分，导致 KaTeX 排版出一个漂移的编号。
    """
    m = _EQ_NUMBER_RE.search(text)
    if m:
        body = text[: m.start()].rstrip()
        if len(body) >= 2:
            return body, m.group(1)
    for m in _EQ_NUMBER_INLINE_RE.finditer(text):
        body = (text[: m.start()] + " " + text[m.end():]).strip()
        body = re.sub(r"\s{2,}", " ", body)
        if len(body) >= 2:
            return body, m.group(1)
    return text, ""


def _local_edges(ordered: list[Line]) -> dict[int, tuple[float, float]]:
    """按「同页同栏、上下相邻几行」估计局部栏边界。

    整页取 min(x0)/max(x1) 会被首页标题、页脚、双栏正文里更宽的行撑大：
    单栏摘要的实际右边界约 469pt，而整页右边界是 505pt，8% 写满容差刚好
    被超过，于是摘要每一行都被判成「段落末行」而一行一块。左边界同理会被
    更靠左的行撑小，使摘要每行都被误判成首行缩进。这里用邻近行的中位数
    估计，既保留「末行短」的识别能力，又不受页面其他区域干扰。
    """
    groups: dict[tuple[int, int], list[Line]] = {}
    for ln in ordered:
        if ln.is_margin:
            continue
        groups.setdefault((ln.page, ln.col), []).append(ln)

    out: dict[int, tuple[float, float]] = {}
    for ls in groups.values():
        ls.sort(key=lambda l: l.y0)
        for i, ln in enumerate(ls):
            win = ls[max(0, i - 2): min(len(ls), i + 3)]
            xs = sorted(w.x1 for w in win)
            # 右边界取 75 分位而不是中位数：表格里的短行/表内标题紧邻
            # 正文长行时，中位数会被连续几个短行拉到同类宽度，导致短行
            # 不再被识别成「段落末行」而与正文粘在一起；75 分位保留长行
            # 的参考作用。首页整段摘要的窗口全为同宽行，结果不受影响。
            right = xs[int(round(0.75 * (len(xs) - 1)))]
            out[id(ln)] = (
                median([w.x0 for w in win]),
                right,
            )
    return out


def _looks_like_body_line(ln: Line, body_size: float) -> bool:
    """图区内仍按正文保留的行：长行，或写满栏宽的多词正文字号行。

    区域层已经会裁掉成段正文，但 `vector_labels` 生长出来的图区可能把
    单行定义/假设段落框进去；这里做第二道保护，避免整行被当图内标注丢掉。
    正文里夹着 κ/λ 等数学字体时 `ln.math` 可能为真，不能因此直接否决；
    只有数学字符占比很高、词数又少的行才是真正的公式/图内标签。
    """
    text = (ln.text or "").strip()
    if not text:
        return False
    words = len(text.split())
    small = body_size > 0 and ln.size < body_size * 0.95
    # 定义/引理/假设段常比正文小一号，但句子完整、词数多，仍按正文保护。
    prose = (
        len(text) >= 80
        and words >= 12
        and re.search(r"[.;:]\s", text)
    )
    if small and not prose:
        return False
    ratio = ln.math_chars / max(ln.total_chars, 1)
    if ratio >= 0.6 and words < 8:
        return False
    if len(text) >= 220:
        return True
    return (ln.bbox[2] - ln.bbox[0]) >= 260 and words >= 8


def _build_blocks(
    ordered: list[Line],
    body_size: float,
    table_rects: dict[int, list[pymupdf.Rect]] | None = None,
    regions: dict[int, PageRegions] | None = None,
    front: FrontMatter | None = None,
) -> list[Block]:
    blocks: list[Block] = []
    cur: dict | None = None
    line_h = median([ln.height for ln in ordered]) if ordered else 12.0

    def in_table(ln: Line) -> bool:
        """行中心是否落在已检测到的表格区域。

        表格行在版面上是一行行短文本、行距紧、数字密集，按正文规则会被
        合并成一整块；`_collect_table_rows` 就再也拿不到「多行短块」，
        Table 1 这类无框表会整张漏掉。表格区内的行一律逐行成块。
        """
        if not table_rects:
            return False
        cx = (ln.bbox[0] + ln.bbox[2]) / 2
        cy = (ln.bbox[1] + ln.bbox[3]) / 2
        for rect in table_rects.get(ln.page, []):
            if rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1:
                return True
        return False

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

    # 整页边界只作兜底；段落合并用附近几行的宽度判断「是否写满 / 缩进」，
    # 避免首页摘要这类窄栏被标题、页脚或双栏正文的宽度误伤。
    local = _local_edges(ordered)

    def flush():
        nonlocal cur
        if cur and cur["text"].strip():
            text = cur["text"].strip()
            lead = len(cur["text"]) - len(cur["text"].lstrip())
            inline_math = []
            for item in cur.get("inline_math") or []:
                start_pos = int(item["start"]) - lead
                end_pos = int(item["end"]) - lead
                if start_pos < 0 or end_pos > len(text) or end_pos <= start_pos:
                    continue
                inline_math.append({**item, "start": start_pos, "end": end_pos})
            blocks.append(
                Block(
                    id="",
                    order=len(blocks),
                    page=cur["page"],
                    kind=cur["kind"],
                    text=text,
                    bbox=cur["bbox"],
                    level=cur["level"],
                    size=cur.get("size", 0.0),
                    bboxes=[tuple(v) for v in cur["bboxes"]],
                    text_rects=[tuple(v) for v in cur.get("text_rects") or []],
                    inline_math=inline_math,
                )
            )
        cur = None

    def start(ln: Line, text: str | None = None, kind: str = "para") -> dict:
        value = text if text is not None else ln.text
        shift = len(ln.text) - len(value) if text is not None and ln.text.endswith(value) else 0
        inline_math = []
        for item in ln.inline_math:
            start_pos = int(item["start"]) - shift
            end_pos = int(item["end"]) - shift
            if start_pos < 0 or end_pos > len(value) or end_pos <= start_pos:
                continue
            inline_math.append({**item, "start": start_pos, "end": end_pos})
        ref_left, ref_right = local.get(id(ln), (ln.x0, ln.x1))
        return {
            "text": value,
            "page": ln.page, "kind": kind, "bbox": ln.bbox, "level": 0,
            "size": ln.size, "bold": ln.bold, "col": ln.col, "mixed": False,
            "bboxes": [[ln.page, ln.bbox[0], ln.bbox[1], ln.bbox[2], ln.bbox[3]]],
            "text_rects": [[ln.page, ln.bbox[0], ln.bbox[1], ln.bbox[2], ln.bbox[3]]],
            "last_y1": ln.y1, "last_x0": ln.x0, "last_x1": ln.x1,
            "ref_left": ref_left, "ref_right": ref_right,
            "in_table": in_table(ln),

            "inline_math": inline_math,
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
        # 通栏摘要带里的期刊页边标记（"Check for updates" 等）不是摘要，
        # 也不该打断摘要段的合并；按 x 重叠判定后直接跳过这一行。
        if (
            front is not None
            and front.page == ln.page
            and front.abstract is not None
            and front.abstract_col < 0
            and front.abstract[0] - 0.5 <= ln.y0 < front.abstract[1]
            and not front.in_abstract(ln)
            and len(ln.text.strip()) <= 40
        ):
            continue

        # 摘要/关键词是「成段的正文」，不是章节标题。期刊常把摘要排成粗体
        # （IEEEtran 的 abstract 就是 \bfseries），如果继续套「粗体短行=标题」
        # 的规则，摘要会一行一个 heading 块，前端渲染成一串小标题。
        abs_head = bool(front and front.page == ln.page and front.is_abstract_head(ln))
        in_abs = bool(front and front.page == ln.page and front.in_abstract(ln))
        in_kw = bool(front and front.page == ln.page and front.in_keywords(ln))
        para_kind = "abstract" if in_abs else ("keywords" if in_kw else "para")

        # 首页前置区（标题 / 作者 / 单位 / 收稿与期刊信息）按行成块，不参与
        # 正文段落合并。AASTeX 这类论文有几十位作者和二十多个单位，按正文
        # 规则会粘成一个上千字符的巨型块：作者识别不出来、单位混进 author，
        # 前端也无法把「开头信息」折叠起来从摘要开始读。
        if (
            front is not None
            and front.page == ln.page
            and (front.in_head(ln.bbox) or front.in_aux(ln.bbox))
            and not in_abs
            and not in_kw
        ):
            flush()
            flush_eq()
            blocks.append(
                Block(
                    id="", order=len(blocks), page=ln.page, kind="para",
                    text=ln.text.strip(), bbox=ln.bbox, level=0, size=ln.size,
                    bboxes=[[ln.page, ln.bbox[0], ln.bbox[1], ln.bbox[2], ln.bbox[3]]],
                )
            )
            continue

        # ── 区域过滤：行号栏与图内文字在建块前就排除 ──
        # 这一步必须在公式/标题判定之前：图里的 `100%`、`Step 3 pool`
        # 既像短段落又像公式碎片，放任它们进入后续规则会同时污染
        # 断段、公式合并与图注配对。图注本身要留下——它是配图的锚点，
        # 也要参与翻译。
        page_regions = regions.get(ln.page) if regions else None
        if page_regions is not None:
            if page_regions.is_line_number(ln.bbox):
                continue
            if (
                page_regions.figure_at(ln.bbox) is not None
                and not _is_caption_text(ln.text)
                and not _looks_like_body_line(ln, body_size)
            ):
                continue

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
            # 只有带数学证据的短碎片才允许被上一条公式吸收；否则紧贴公式
            # 的正文短句（如 "Table 2 lists..."）会被整句并进公式裁图。
            and bool(ln.math or ln.inline_math or _MATH_SYMBOL_RE.search(ln.text))
            and len(_WORD_RE.findall(ln.text)) <= 6
        )
        if (_looks_equation(ln.text, ln.math, ln.inline_math) or absorb) and not _is_heading(ln, body_size):
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

        # 表格区里的短行（「3.2 分（1～5 分）」）会被中文编号标题规则误判，
        # 表格行一律按表格数据处理，不参与标题判定。摘要正文与关键词常是
        # 粗体、行尾无标点，同样不能当标题；只有 Abstract 标题行本身例外。
        head_level = 0 if (
            in_table(ln) or (in_abs and not abs_head) or in_kw
        ) else _is_heading(ln, body_size)
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
            cur = start(ln, rest, para_kind)
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

        # 摘要 → 关键词 → 正文的切换必须断开段落；否则关键词会被并进摘要
        # 末段，摘要译文里多出一串关键词。
        if cur is not None and cur["kind"] != para_kind:
            flush()
        if cur is None:
            cur = start(ln, kind=para_kind)
            continue

        # 表格区/大间距碎片逐行成块：既不让表格行彼此合并，也不让
        # 表格行并进正文段落；否则兜底切表拿不到「多行短块」。
        # 小字号短行（表格数据、脚注）同样逐行成块，避免整张表粘成一段。
        def small_row(text: str, size: float) -> bool:
            """小字号短行：表格数据行 / 大写字表头。

            不能只按字号判断——参考文献也用小字号，逐行断开会让每条文献
            碎成好几块。用「短 + 数字或全大写词密集 + 不含年份」限定到表格。
            """
            t = text.strip()
            if not (body_size > 0 and 0 < size < body_size * 0.9) or not t or len(t) > 120:
                return False
            if re.match(r"^\s*(?:\[\d{1,3}\]|\d{1,3}\.\s)", t):
                return False
            if re.search(r"\b(?:19|20)\d{2}[a-z]?\b", t):
                return False
            digits = sum(1 for ch in t if ch.isdigit())
            caps = len(re.findall(r"\b[A-Z]{2,}\b", t))
            return digits >= 2 or caps >= 2

        if not (in_abs or in_kw) and (
            in_table(ln)
            or cur["in_table"]
            or small_row(ln.text, ln.size)
            or small_row(cur["text"], cur["size"])
        ):
            flush()
            cur = start(ln, kind=para_kind)
            continue

        # 上一行是否写满，要用「上一行附近几行」的宽度判断，而不是整页
        # 该栏的极值；否则首页窄摘要会被标题/页脚撑出的右边界误判成没写满。
        prev_left, prev_right = cur["ref_left"], cur["ref_right"]
        prev_short = cur["last_x1"] < prev_right - 0.08 * max(prev_right - prev_left, 1.0)
        gap = ln.y0 - cur["last_y1"]
        # 两端对齐的正文里，行尾因长单词换行变短并不等于段落结束。
        # 「上一行短」必须和句末标点 / 下一行大写或数字 / 较大段间距
        # 同时成立才断段；否则下一行小写接续词仍属于同一段。
        next_starts_new = bool(
            ln.text[:1].isupper()
            or ln.text[:1].isdigit()
            or ln.text[:1] in "(["
        )
        prev_short_break = prev_short and (
            _ends_sentence(cur["text"])
            or next_starts_new
            or gap > max(line_h * 1.35, 8.0)
        )
        left, right = local.get(id(ln), edges.get((ln.page, ln.col), (ln.x0, ln.x1)))
        # 首行缩进：既要相对栏左边界缩进，又要相对上一行缩进（否则整段每行都会被误判）
        indented = (
            ln.x0 - left > 0.8 * ln.size
            and ln.x0 - cur["last_x0"] > 0.5 * ln.size
        )


        same_style = (
            abs(ln.size - cur["size"]) <= 1.0 and ln.bold == cur["bold"]
        )
        # 跨栏（左→右、右→下一页左栏）和跨页的断行：满足「上一行写满 +
        # 上一段没说完 + 本行小写/接续符开头 + 无首行缩进」就接回同一段。
        # 只在有明确接续证据时合并，避免把相邻的两个段落粘在一起。
        cross = ln.page != cur["page"] or ln.col != cur["col"]
        # 同页「左栏→右栏」只能靠强证据接续：两栏是独立阅读流，左栏末行
        # 只是没有句号的普通行（PDF 抽取很常见），不足以说明段落延续到右栏。
        # 按普通续段处理会把右栏首段整段并进左栏末段，正是串行错拼的来源。
        same_page_cross = cross and ln.page == cur["page"]
        strong_cross = cur["text"].rstrip()[-1:] in ",;:，；：-\u2010\u2011"
        continuation = (
            cross
            and same_style
            and not prev_short
            and not indented
            and _continues_paragraph(cur["text"], ln.text)
            and (not same_page_cross or strong_cross)
        )

        if (in_abs or in_kw) and cur is not None and cur["kind"] == para_kind:
            # 摘要/关键词是一段连续文字，不受正文的「是否写满/首行缩进/
            # 换栏」规则影响。Nature 这类通栏摘要经 _reading_order 归栏后
            # 相邻行 col 会变，若沿用正文断段规则会被拆成一行一块。
            new_para = not (same_style and gap <= max(line_h * 1.9, 8.0))
        else:
            new_para = (not continuation) and (
                ln.col != cur["col"]
                or gap > max(line_h * 1.55, 6)
                or not same_style
                or prev_short_break
                or indented
                # 图注比正文短，局部宽度下末行容易被判成「写满」而把紧随的
                # 作者脚注、版权行吞进 caption。只有明确的续行证据才允许合并，
                # 图注以句号结尾或下一行以 ∗ / ‡ / 数字开头时一律断开。
                or (_is_caption_text(cur["text"]) and not _caption_continuation(cur["text"], ln.text))
                # 同栏里上一行「写满」时，紧接的表注会被当成正文续行并进
                # 表格末行，`Table 2:` 随之失去锚点，整张表识别不出来。
                or _is_caption_text(ln.text)
            )
        if new_para:
            flush()
            cur = start(ln, kind=para_kind)
        else:
            lead = len(ln.text) - len(ln.text.lstrip())
            cur["text"], offset = _append_line_text(cur["text"], ln.text)
            for item in ln.inline_math:
                start_pos = int(item["start"]) - lead + offset
                end_pos = int(item["end"]) - lead + offset
                if start_pos < 0 or end_pos <= start_pos:
                    continue
                cur["inline_math"].append(
                    {**item, "start": start_pos, "end": end_pos}
                )
            _push_bbox(cur["bboxes"], ln.page, ln.bbox)
            cur["text_rects"].append([ln.page, ln.bbox[0], ln.bbox[1], ln.bbox[2], ln.bbox[3]])
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
            if id(ln) in local:
                cur["ref_left"], cur["ref_right"] = local[id(ln)]
            if ln.page == cur["page"]:
                cur["col"] = ln.col
    flush_eq()
    flush()
    # 兜底：标题、图注、表格、公式等直接构造的块没有逐行累积，用 bbox 补一条。
    # 正文段落块已在 start()/续行处逐行记录，这里不会覆盖。
    for block in blocks:
        if not block.text_rects and block.bbox:
            block.text_rects = [
                (
                    int(item[0]),
                    float(item[1]),
                    float(item[2]),
                    float(item[3]),
                    float(item[4]),
                )
                for item in (block.bboxes or [[block.page, *block.bbox]])
            ]
        elif block.text_rects and block.bbox:
            # 解析行常把「左侧信息栏 + 右侧正文」拼成同一行，行矩形因此横跨
            # 整页。块自己的 bbox 才是它的列范围，必须把行矩形裁到块横向范围，
            # 否则 affiliation/ORCID 的 text_rects 会盖住整页正文，契约模式下
            # 两段中文写在同一位置。
            left = float(block.bbox[0]) - 1.5
            right = float(block.bbox[2]) + 1.5
            clipped: list[tuple] = []
            for rect in block.text_rects:
                page_no, x0, y0, x1, y1 = rect[0], *rect[1:]
                nx0 = max(float(x0), left)
                nx1 = min(float(x1), right)
                if nx1 - nx0 > 1:
                    clipped.append(
                        (int(page_no), nx0, float(y0), nx1, float(y1))
                    )
            if clipped:
                block.text_rects = clipped
    return blocks


def _clip_text_rects_to_bbox(blocks: list[Block]) -> list[Block]:
    """把逐行 text_rects 裁到块最终 bbox 的横向范围。

    首页「左信息栏 + 右正文」在解析行层面是同一行（x 跨整页），块的 bbox
    要到列拆分后才收窄。必须在流水线末尾按最终 bbox 再裁一次，否则
    affiliation/ORCID 的行矩形会盖住正文，契约模式把两段中文写在同一处。
    """
    for block in blocks:
        if not block.text_rects or not block.bbox:
            continue
        left = float(block.bbox[0]) - 1.5
        right = float(block.bbox[2]) + 1.5
        if right <= left:
            continue
        clipped: list[tuple] = []
        for rect in block.text_rects:
            page_no, x0, y0, x1, y1 = rect[0], *rect[1:]
            nx0 = max(float(x0), left)
            nx1 = min(float(x1), right)
            # 行矩形上下各内缩 1.0pt：相邻块共享行边界时只是「bbox 相切」，
            # 不缩会一直被算成重叠；内缩后仍由擦除 inflate 0.8 兜住字形边缘。
            ny0 = float(y0) + 1.0
            ny1 = float(y1) - 1.0
            if nx1 - nx0 > 1 and ny1 - ny0 > 1:
                clipped.append((int(page_no), nx0, ny0, nx1, ny1))
        if clipped:
            block.text_rects = clipped
    return blocks


def _drop_covered_blocks(blocks: list[Block]) -> list[Block]:
    """把「所有行都被另一个更大的块覆盖」的块标为不可译。

    解析偶发把一个段落中的一行同时并进 heading（p18 基金声明实测），两块
    各写一份译文就会在同一行叠字。大块会擦除并重写这段区域，小块不再单独
    回填即可；不置空 text_rects，高亮定位仍可用。
    """
    for block in blocks:
        if (
            not block.translatable
            or block.kind in ("figure", "table", "equation", "reference", "margin")
            or not block.text_rects
        ):
            continue
        own = [
            pymupdf.Rect(rect[1], rect[2], rect[3], rect[4])
            for rect in block.text_rects
            if int(rect[0]) == block.page
        ]
        if not own:
            continue
        for other in blocks:
            if other is block or other.page != block.page:
                continue
            if other.kind in ("figure", "table", "equation", "reference", "margin"):
                continue
            if len(other.text) <= len(block.text):
                continue
            cover = [
                pymupdf.Rect(rect[1], rect[2], rect[3], rect[4])
                for rect in (other.text_rects or [])
                if int(rect[0]) == block.page
            ]
            if not cover:
                continue
            covered = 0
            for rect in own:
                area = rect.get_area()
                if area <= 0:
                    continue
                if any(
                    not (rect & band).is_empty
                    and (rect & band).get_area() > area * 0.5
                    for band in cover
                ):
                    covered += 1
            if covered == len(own):
                block.translatable = False
                break
    return blocks


# ── 块分类与清理 ───────────────────────────────────────────────────────

_AFFIL_RE = re.compile(
    r"Department|University|Universit|Institute|Institut|Laborator|College|Academy|"
    r"School of|Inc\.|Corp|Ltd|Research Center|Research Centre|Observator|"
    r"Telescope|Astronom|Astrophys|Nucleus|Facility|@",
    re.IGNORECASE,
)
# 单位列表常见形态：上标编号粘在机构名前，如 "adComprehensive Cancer Center"
_AFFIL_MARKER_RE = re.compile(r"^[a-z]{1,3}[A-Z][a-z]")
_KEYWORDS_RE = re.compile(r"^(?:Keywords?|KEY\s*WORDS|Index Terms)\s*[:：]", re.IGNORECASE)
_NAME_RE = re.compile(r"[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z]+")
# 姓名里的姓氏小品词（Paul P. van der Werf / Ludwig van Beethoven）。
# 它们是小写，但属于姓名的一部分，不能因此把整行判成单位/正文。
_NAME_PARTICLES = re.compile(
    r"^(?:van|von|de|del|della|der|da|di|du|la|le|ten|ter|dos|das|bin|al)$",
    re.IGNORECASE,
)
_FOOTNOTE_RE = re.compile(
    r"copyright\s*©|all rights reserved|contributed equally|"
    r"permission to make digital|^proceedings of the|^arxiv:\s*\d|"
    r"under review|^doi:",
    re.IGNORECASE,
)
# 首页/次页底部的作者脚注：单位、邮箱、通讯作者。它们是元数据，不是正文，
# 不能参与翻译；有些期刊没有 affiliation 标签，靠这些短语识别。
_AUTHOR_NOTE_RE = re.compile(
    r"(?:is\s+with\s+the|are\s+with\s+the|Corresponding\s+author|"
    r"E-?mail\s*[:：]|@|\bNSF\b|\bNASA\b|\bFellow(?:ship)?\b)",
    re.IGNORECASE,
)
# 文末/页底的作者简介与声明：和作者单位一样属于元数据，阅读时折叠展示，
# 不进正文流、不参与翻译。
_TAIL_META_HEAD_RE = re.compile(
    r"^(?:[\d*†‡§¶#]+\s*)?(?:"
    r"credit\s+authorship\s+contribution(?:\s+statement)?|"
    r"authorship\s+contribution\s+statement|"
    r"authors?\s+contributions?|"
    r"funding(?:\s+and\s+acknowledg(?:e)?ments?)?|"
    r"competing\s+interests?|"
    r"conflicts?\s+of\s+interest|"
    r"declaration\s+of\s+(?:competing\s+)?interests?|"
    r"(?:data|code)\s+availability(?:\s+statement)?|"
    r"availability\s+of\s+(?:data|code|materials)|"
    r"acknowledg(?:e)?ments?(?:\s+and\s+disclosure\s+of\s+funding)?|"
    r"author\s+(?:biograph(?:y|ies)|information)|"
    r"about\s+the\s+authors?|biograph(?:y|ies)|"
    r"corresponding\s+authors?|"
    r"orcid|e-?mail\s+addresses?|"
    r"supplementary\s+material|supporting\s+information|"
    r"ethics\s+(?:statement|approval)|"
    r"consent\s+(?:to\s+participate|for\s+publication)|"
    r"patient\s+consent|"
    r"received|accepted|revised|published"
    r")\b",
    re.IGNORECASE,
)
_CONTACT_RE = re.compile(
    r"(?:Corresponding\s+authors?|E-?mail\s*(?:address(?:es)?)?\s*[:：]|"
    r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}|\bORCID\s*(?:\(s\))?\s*[:：])",
    re.IGNORECASE,
)
_DECLARATION_RE = re.compile(
    r"\b(?:supported|funded|grant|award|declare|declared|contributed|contribution|"
    r"designed|wrote|acknowledge|available|approved|consent|competing|interests?|"
    r"initiated|conceptualization|methodology|investigation|supervised|performed|"
    r"analy[sz]ed)\b",
    re.IGNORECASE,
)
_DECL_START_RE = re.compile(r"^(?:This|These|The|Our|We|It|All|No)\b")
_CREDIT_ACTION_RE = re.compile(
    r"\b(?:designed|wrote|conceived|performed|analy[sz]ed|initiated|supervised|"
    r"contributed|contribution|conceptualization|methodology|investigation)\b",
    re.IGNORECASE,
)
_BIO_RE = re.compile(
    r"(?:received\s+(?:his|her|their)\s+(?:Ph\.?\s?D|B\.?\s?S|M\.?\s?S|M\.?D|doctor|bachelor|master)|"
    r"(?:his|her|their)\s+research\s+interests|"
    r"research\s+interests\s+(?:include|are|focus)|"
    r"is\s+(?:a|an)\s+(?:full\s+|associate\s+|assistant\s+)?"
    r"(?:professor|researcher|lecturer|scientist|engineer|physician)|"
    r"works?\s+(?:at|in)\s+(?:the\s+)?"
    r"(?:department|institute|university|college|laboratory))",
    re.IGNORECASE,
)


def _is_tail_metadata(
    b: Block,
    text: str,
    body_size: float,
    page_height: float,
    last_page: int,
    in_refs: bool,
) -> bool:
    """判断块是不是文末/页底的作者简介、通讯方式或声明类信息。"""
    if not text or len(text) > 1600:
        return False
    head = _TAIL_META_HEAD_RE.match(text)
    head_hit = head is not None
    bio_hit = _BIO_RE.search(text) is not None
    contact_hit = _CONTACT_RE.search(text) is not None
    if not (head_hit or bio_hit or contact_hit):
        return False
    if _REF_START_RE.match(text) and _REF_YEAR_RE.search(text) and not head_hit:
        return False
    bottom = b.bbox[3] >= page_height * 0.68
    small = body_size > 0 and b.size > 0 and b.size < body_size * 0.95
    if head_hit:
        rest = text[head.end():].lstrip()
        if not rest or rest[:1] in ":：":
            return True
        # 段落型声明：标签后要能看出是「声明内容」（This work was supported /
        # The authors declare / 作者贡献动作），而不是以 Funding/Data 开头的正文句。
        declared = bool(_DECLARATION_RE.search(rest)) and (
            bool(_DECL_START_RE.match(rest)) or bool(_CREDIT_ACTION_RE.search(rest)) or in_refs
        )
        if not declared:
            return False
        return bottom or small or in_refs or b.page >= max(1, last_page - 1)
    return (bottom or small) and (bio_hit or contact_hit)


def _pick_title_index(blocks: list[Block], page_height: float) -> int | None:
    """选出标题块：首页上部、字号最大、排除 DOI/URL/页眉行。

    不能简单取第一个块——很多期刊首页首行是 DOI、栏目名或下载水印。
    """
    best: tuple[tuple[float, float], int] | None = None
    head = blocks[:16]
    sizes = [b.size for b in head if b.size > 0]
    # 首页没有真正的大字号标题时（只有正文段的预印本/测试版式），不能
    # 把第一个短块硬当标题：表格行、图内标注会被误选，随后图注被并进
    # 标题、图/表失去锚点。只有明显大于页面常规字号的块才是标题候选。
    base_size = median(sizes) if sizes else 0.0
    for i, b in enumerate(head):
        if b.page != 1:
            continue
        t = b.text.strip()
        if len(t) < 12 or len(t) > 300:
            continue
        if base_size > 0 and b.size < base_size * 1.05:
            continue
        if re.match(r"^(https?://|doi:|downloaded\s+from|received:|accepted:)", t, re.IGNORECASE):
            continue
        if b.bbox[1] > page_height * 0.45:
            continue
        if b.kind == "equation":
            continue
        # 首页的图表注不是论文标题（图形摘要旁的 "Table 1" 常被误选）
        if _CAPTION_RE.match(t) and _is_caption_text(t):
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
        t.bboxes = _merge_bbox_lists(t.bboxes, n.bboxes)
        t.bbox = (
            min(t.bbox[0], n.bbox[0]), min(t.bbox[1], n.bbox[1]),
            max(t.bbox[2], n.bbox[2]), max(t.bbox[3], n.bbox[3]),
        )
        del blocks[j]


def _looks_author_name(text: str) -> bool:
    """是不是一行作者姓名（而不是单位/邮箱/城市/正文）。

    面向通用版式，不写死某套模板：
    - 先剥掉 1st/2nd、上标数字、† 等作者序号；
    - 单位/邮箱/网址/国家城市直接排除；
    - 英文要求至少两个词且首字母大写（挡住 "Technology" 这类单词），
      中文要求 2~6 个汉字。
    """
    raw = _AUTHOR_PREFIX_RE.sub("", text.strip()).strip(" ,;*†‡§¶#")
    if not raw or len(raw) > 400:
        return False
    if (
        _AFFIL_RE.search(raw)
        or "@" in raw
        or re.search(r"https?://|www\.|doi:", raw, re.IGNORECASE)
        or _PLACE_RE.search(raw)
        or re.search(r"\d{4,}", raw)
        or re.search(r":|：", raw)
        # 期刊栏目名（R E V I E W / REVIEW / ARTICLE）不是作者
        or re.fullmatch(r"(?:[A-Z]\s*){2,}", raw)
        or re.fullmatch(
            r"(?i)(?:review|article|letter|communication|editorial|perspective|"
            r"comment|original\s+research|short\s+communication)",
            raw,
        )
        or re.search(
            r"\b(?:Received|Accepted|Published|Revised|Online|Open\s+access|"
            r"Submitted|Preprint|Draft\s+version|Typeset|ApJ|ApJL|MNRAS|AASTeX|"
            r"January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\b",
            raw,
            re.IGNORECASE,
        )
    ):
        return False
    cjk = re.findall(r"[\u3400-\u9fff]", raw)
    if cjk:
        return 2 <= len(cjk) <= 6 and not re.search(r"[A-Za-z]{3,}", raw)

    # 一行多位作者（Nature/Science 常见）：按逗号/and 分段，只要过半片段
    # 是「首字母大写的人名」，整行就是作者行。不要因为行很长就放弃。
    parts = [
        p.strip()
        for p in re.split(r"\s*(?:,|、|;|&|\band\b)\s*", raw)
        if p.strip()
    ]
    if len(parts) >= 2:
        name_like = 0
        for part in parts:
            if re.search(
                r"\b(?:Member|Student|IEEE|ACM|Fellow|Editor|Corresponding)\b",
                part,
                re.IGNORECASE,
            ):
                continue
            ws = re.findall(r"[A-Za-z][A-Za-z'’\-.]*", part)
            if not (1 <= len(ws) <= 5):
                continue
            caps = sum(
                1
                for w in ws
                if w[:1].isupper() or w.isupper() or _NAME_PARTICLES.match(w)
            )
            if caps >= len(ws) - 1:
                name_like += 1
        if name_like >= 2:
            return True

    words: list[str] = []
    for part in parts:
        words.extend(re.findall(r"[A-Za-z][A-Za-z'’\-.]*", part))
    if not (2 <= len(words) <= 8):
        return False
    if re.search(
        r"\b(?:School|Department|University|Institute|College|Laborator|Center|Lab)\b",
        raw,
        re.IGNORECASE,
    ):
        return False
    caps = sum(
        1 for w in words
        if w[:1].isupper() or w.isupper() or _NAME_PARTICLES.match(w)
    )
    return caps >= len(words) - 1


def _card_block(card: list[Block]) -> Block:
    """把同一张作者卡片的多行合并成一个块（姓名 + 单位 + 邮箱）。"""
    text = card[0].text
    bbox = card[0].bbox
    bboxes = list(card[0].bboxes)
    for b in card[1:]:
        text = _join_text(text, b.text)
        bbox = (
            min(bbox[0], b.bbox[0]), min(bbox[1], b.bbox[1]),
            max(bbox[2], b.bbox[2]), max(bbox[3], b.bbox[3]),
        )
        bboxes = _merge_bbox_lists(bboxes, b.bboxes)
    kind = "author" if any(_looks_author_name(b.text) for b in card) else "affiliation"
    return Block(
        id="", order=0, page=card[0].page, kind=kind, text=text,
        bbox=bbox, size=max(b.size for b in card), bboxes=bboxes,
        translatable=False,
    )


# L2 列模型：页缘信息栏（作者单位 / 期刊信息 / 资助声明）判据。
# 只用解析行数据判定，供拆块与译文排版共用同一套几何。
SIDE_RATIO_MIN = 0.06
SIDE_RATIO_MAX = 0.22
SIDE_EDGE_RATIO = 0.32
SIDE_MIN_LINES = 3
SIDE_X1_SPREAD = 12.0
SIDE_Y_GAP = 40.0
SIDE_COLUMN_GAP = 6.0
SIDE_BODY_MIN_RATIO = 0.34
SIDE_PROSE_CHARS = 120


def _looks_like_prose_text(text: str) -> bool:
    """成段正文不能当信息栏（>=120 字符且带句末标点）。"""
    cleaned = " ".join((text or "").split())
    if len(cleaned) < SIDE_PROSE_CHARS:
        return False
    stripped = cleaned.rstrip()
    return stripped[-1:] in "。！？.!?" or stripped.count(". ") >= 2


def _line_clusters_by_x1(lines: list[Line]) -> list[list[Line]]:
    """按右缘聚类：信息栏多为居中短行，左缘随文本长度变化，右缘才稳定。"""
    ordered = sorted(lines, key=lambda ln: ln.x1)
    clusters: list[list[Line]] = []
    for ln in ordered:
        if clusters and abs(ln.x1 - clusters[-1][-1].x1) <= SIDE_X1_SPREAD:
            clusters[-1].append(ln)
        else:
            clusters.append([ln])
    return clusters


def _longest_vertical_chain(lines: list[Line]) -> list[Line]:
    chain: list[Line] = []
    best: list[Line] = []
    for ln in sorted(lines, key=lambda item: item.y0):
        if chain and ln.y0 - chain[-1].y0 > SIDE_Y_GAP:
            if len(chain) > len(best):
                best = chain
            chain = []
        chain.append(ln)
    return chain if len(chain) > len(best) else best


def _detect_side_bands(
    ordered: list[Line], page_width: float
) -> dict[int, list[tuple[float, float]]]:
    """每页的页缘信息栏 x 区间（左/右各至多一条）。

    判据：列宽 0.06-0.22 页宽、右缘对齐、>=3 行纵向连续、与正文列有
    >=6pt 空白沟、页面存在更宽的正文行；成段正文一票否决。
    """
    if page_width <= 0:
        return {}
    by_page: dict[int, list[Line]] = {}
    for ln in ordered:
        if ln.is_margin:
            continue
        by_page.setdefault(ln.page, []).append(ln)
    result: dict[int, list[tuple[float, float]]] = {}
    for page, lines in by_page.items():
        bands: list[tuple[float, float]] = []
        for side in ("left", "right"):
            edge = [
                ln
                for ln in lines
                if (
                    (ln.x0 + ln.x1) / 2 < page_width * SIDE_EDGE_RATIO
                    if side == "left"
                    else (ln.x0 + ln.x1) / 2 > page_width * (1 - SIDE_EDGE_RATIO)
                )
            ]
            if len(edge) < SIDE_MIN_LINES:
                continue
            cluster = max(_line_clusters_by_x1(edge), key=len)
            chain = _longest_vertical_chain(cluster)
            if len(chain) < SIDE_MIN_LINES:
                continue
            x0 = min(ln.x0 for ln in chain)
            x1 = max(ln.x1 for ln in chain)
            width = x1 - x0
            if width < page_width * SIDE_RATIO_MIN:
                continue
            if width > page_width * SIDE_RATIO_MAX:
                continue
            if max(ln.x1 for ln in chain) - min(ln.x1 for ln in chain) > SIDE_X1_SPREAD:
                continue
            if any(_looks_like_prose_text(ln.text) for ln in chain):
                continue
            chain_ids = {id(ln) for ln in chain}
            rest = [ln for ln in lines if id(ln) not in chain_ids]
            if not rest:
                continue
            if not any(ln.width >= page_width * SIDE_BODY_MIN_RATIO for ln in rest):
                continue
            if side == "left":
                right_edges = [ln.x0 for ln in rest if ln.x0 >= x1 - 2]
                if not right_edges or min(right_edges) - x1 < SIDE_COLUMN_GAP:
                    continue
                bands.append((0.0, x1 + 0.5))
            else:
                left_edges = [ln.x1 for ln in rest if ln.x1 <= x0 + 2]
                if not left_edges or x0 - max(left_edges) < SIDE_COLUMN_GAP:
                    continue
                bands.append((x0 - 0.5, page_width))
        if bands:
            result[page] = bands
    return result


def _column_bounds(
    page_lines: list[Line],
    gutter: float | None,
    side_bands: list[tuple[float, float]],
    page_width: float,
) -> list[tuple[float, float]]:
    """把「双栏缝 + 侧栏边界」合成页面列区间（按内容区裁剪、去掉过窄段）。"""
    if not page_lines:
        return []
    content_x0 = min(ln.x0 for ln in page_lines)
    content_x1 = max(ln.x1 for ln in page_lines)
    cuts: list[float] = []
    if gutter is not None:
        cuts.append(gutter)
    for band_x0, band_x1 in side_bands:
        cuts.extend((band_x0, band_x1))
    cuts = sorted(
        {
            round(value, 2)
            for value in cuts
            if content_x0 + 8 < value < content_x1 - 8
        }
    )
    columns: list[tuple[float, float]] = []
    left = content_x0
    for cut in cuts:
        if cut - left >= 8:
            columns.append((left, cut))
            left = cut
    if content_x1 - left >= 8:
        columns.append((left, content_x1))
    if page_width > 0 and not columns:
        columns.append((content_x0, content_x1))
    return columns


def _line_spans_columns(ln: Line, columns: list[tuple[float, float]]) -> bool:
    """一行是否自身横跨 >=2 个列区间（citation 与正文被并进同一行）。"""
    covered = 0
    for c0, c1 in columns:
        if min(ln.x1, c1) - max(ln.x0, c0) > (c1 - c0) * 0.3:
            covered += 1
    return covered >= 2


def _parts_from_words(
    rect: pymupdf.Rect,
    columns: list[tuple[float, float]],
    words: list[tuple],
) -> list[tuple[int, str, tuple[float, float, float, float]]] | None:
    """按 word 的 x 中心归列（行本身交错时的唯一可靠拆分口径）。"""
    if not words:
        return None
    buckets: dict[int, list[tuple]] = {index: [] for index in range(len(columns))}
    for word in words:
        x0, y0, x1, y1 = (float(value) for value in word[:4])
        center_x = (x0 + x1) / 2
        center_y = (y0 + y1) / 2
        if not (
            rect.x0 - 2 <= center_x <= rect.x1 + 2
            and rect.y0 - 2 <= center_y <= rect.y1 + 2
        ):
            continue
        target = next(
            (
                index
                for index, (c0, c1) in enumerate(columns)
                if c0 - 1 <= center_x <= c1 + 1
            ),
            None,
        )
        if target is None:
            return None
        buckets[target].append((x0, y0, x1, y1, str(word[4])))
    buckets = {index: group for index, group in buckets.items() if group}
    if len(buckets) < 2:
        return None
    parts: list[tuple[int, str, tuple[float, float, float, float]]] = []
    for index in sorted(buckets):
        group = sorted(buckets[index], key=lambda item: (round(item[1], 1), item[0]))
        text = " ".join(item[4] for item in group)
        bbox = (
            min(item[0] for item in group),
            min(item[1] for item in group),
            max(item[2] for item in group),
            max(item[3] for item in group),
        )
        parts.append((index, text, bbox))
    return parts


_SIDEBAR_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SIDEBAR_URL_RE = re.compile(
    r"https?://|www\.|doi\.org/|orcid\.org/", re.IGNORECASE
)
_SIDEBAR_DOI_RE = re.compile(r"\b10\.\d{4,9}/", re.IGNORECASE)


def _is_sidebar_whitelist(text: str) -> bool:
    """侧栏里不需要翻译的行：邮箱、URL、DOI/ORCID、纯数字符号。

    机构、期刊栏目、收稿/接收/出版日期、citation 文本都要译；只有这些
    没有语义可翻的联系方式/标识保留原文。
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return True
    if _SIDEBAR_EMAIL_RE.search(cleaned):
        return True
    if _SIDEBAR_URL_RE.search(cleaned):
        return True
    if _SIDEBAR_DOI_RE.search(cleaned):
        return True
    if re.fullmatch(r"[\d\s.,;:%±×–—/()\[\]-]+", cleaned):
        return True
    return False


# 跨列拆分候选：这四类都可能被解析按「同一视觉行」拼成整页宽块——首页
# 摘要旁边是审稿人卡片、作者单位旁边是正文、页脚日期旁边是正文结尾。
# title / heading / caption 不拆：拆开会破坏标题层级与图注锚点。
_SPLITTABLE_KINDS = ("para", "abstract", "affiliation", "footnote")
# 块在某一列上的覆盖不足这个比例就不算「跨列块」：纯正文列的单位块宽度
# 常超过半页宽，但它只落在一列里，不该进入拆分。
_CROSS_COLUMN_MIN_SHARE = 0.15
# 首页信息栏的「标签词」：这些词只出现在页缘信息栏，拆分时可能因为词框
# 跨过列界被归进正文片段。它们是封闭的版式词汇，不是某一篇论文的特例。
_FRONT_LABEL_RE = re.compile(
    r"^(?:Citation|Edited\s+by|Reviewed\s+by|Specialty\s+section|Received|"
    r"Accepted|Published|Keywords|Correspondence)\s*[:：]\s*",
    re.IGNORECASE,
)


def _split_cross_column_blocks(
    blocks: list[Block],
    ordered: list[Line],
    gutters: dict[int, float | None],
    page_width: float,
    side_bands: dict[int, list[tuple[float, float]]] | None = None,
    doc=None,
) -> list[Block]:
    """L2：把横跨列边界的块拆回各自列（跨列块拆分）。

    列边界 = 双栏 gutter ∪ 页缘信息栏边界；首页「单栏正文 + 左侧信息栏」
    的混合块也能拆开，不再整块 preserved。

    两档拆分：
      * 行级：块内每一行都只落在一个列区间时，按行分组拼接；
      * word 级：行本身横跨两列（citation 与正文并进同一行）时，按每个 word
        的 x 中心归列——这是 b0024 型首页混合块的唯一可靠口径。

    约束：只拆 para；跨页块、含跨栏行或行内公式的块不拆；每个子块去空白后
    >=20 字符；守恒校验用字符多重集合（不是词数）；落到信息栏区间的子块
    标记 column_role=sidebar 且不可译（保留原文）。
    """
    if not blocks or page_width <= 0:
        return blocks
    side_bands = side_bands or {}
    by_page: dict[int, list[Line]] = {}
    for ln in ordered:
        # 页眉页脚横贯整页，会把内容区拉宽、凭空造出「列」，必须排除。
        if ln.is_margin:
            continue
        by_page.setdefault(ln.page, []).append(ln)

    def norm(text: str) -> str:
        return "".join(text.split())

    # 按块所在页取词：跨列拆分可能在整篇 blocks 上统一执行，不能复用
    # 循环变量 page（它指向最后一页），否则会用错页的词拆块。
    word_cache: dict[int, list[tuple]] = {}

    def page_words(page_no: int) -> list[tuple]:
        if doc is None or not (1 <= page_no <= len(doc)):
            return []
        if page_no not in word_cache:
            word_cache[page_no] = doc[page_no - 1].get_text("words")
        return word_cache[page_no]

    out: list[Block] = []
    for block in blocks:
        if (
            block.kind not in _SPLITTABLE_KINDS
            or len(block.bboxes) > 1
            or block.inline_math
            or block.bbox[2] - block.bbox[0] <= page_width * 0.5
        ):
            out.append(block)
            continue
        page_lines = by_page.get(block.page, [])
        rect = pymupdf.Rect(block.bbox)
        inner = [
            ln
            for ln in page_lines
            if rect.x0 - 2 <= (ln.x0 + ln.x1) / 2 <= rect.x1 + 2
            and rect.y0 - 2 <= (ln.y0 + ln.y1) / 2 <= rect.y1 + 2
        ]
        inner.sort(key=lambda ln: (ln.baseline or ln.y0, ln.x0))
        if len(inner) < 1 or any(ln.col < 0 for ln in inner):
            out.append(block)
            continue
        columns = _column_bounds(
            page_lines,
            gutters.get(block.page),
            side_bands.get(block.page, []),
            page_width,
        )
        if len(columns) < 2:
            out.append(block)
            continue
        # 只落在单一列里的「宽块」（正文列的单位行、通栏标题）不是跨列块，
        # 别让它白跑一遍行级/词级拆分。
        rect_cover = pymupdf.Rect(block.bbox)
        covered_columns = [
            index
            for index, (c0, c1) in enumerate(columns)
            if min(rect_cover.x1, c1) - max(rect_cover.x0, c0)
            > (c1 - c0) * _CROSS_COLUMN_MIN_SHARE
        ]
        if len(covered_columns) < 2:
            out.append(block)
            continue

        parts: list[tuple[int, str, tuple[float, float, float, float]]] | None = None
        line_groups: dict[int, list[Line]] | None = {
            index: [] for index in range(len(columns))
        }
        for ln in inner:
            center = (ln.x0 + ln.x1) / 2
            target = next(
                (
                    index
                    for index, (c0, c1) in enumerate(columns)
                    if c0 - 1 <= center <= c1 + 1
                ),
                None,
            )
            if target is None:
                line_groups = None
                break
            line_groups[target].append(ln)
        if line_groups is not None:
            line_groups = {index: group for index, group in line_groups.items() if group}
            # 任一行横跨两列 → 行级分组不可靠，交给 word 级。
            if len(line_groups) >= 2 and not any(
                _line_spans_columns(ln, columns) for ln in inner
            ):
                parts = []
                for index, group in line_groups.items():
                    text = group[0].text
                    for ln in group[1:]:
                        text = _join_text(text, ln.text)
                    bbox = (
                        min(ln.x0 for ln in group),
                        min(ln.y0 for ln in group),
                        max(ln.x1 for ln in group),
                        max(ln.y1 for ln in group),
                    )
                    parts.append((index, text, bbox))
        if parts is None:
            parts = _parts_from_words(rect, columns, page_words(block.page))
        if not parts:
            out.append(block)
            continue
        bands = side_bands.get(block.page, [])

        def _on_side_part(index: int) -> bool:
            c0, c1 = columns[index]
            return any(
                min(c1, band_x1) - max(c0, band_x0) > (c1 - c0) * 0.5
                for band_x0, band_x1 in bands
            )

        # 信息栏里的孤立短标签（Citation: / Edited by:）不值得单独成块，但
        # 它们在原文里占字符：直接丢掉会让整块被判「不守恒」而放弃拆分，
        # 首页混合块就永远拆不开。丢掉的部分记进守恒计数。
        dropped = Counter()
        kept_parts: list[tuple[int, str, tuple[float, float, float, float]]] = []
        for part in parts:
            if len(norm(part[1])) < 20 and _on_side_part(part[0]):
                dropped += Counter(norm(part[1]))
                continue
            kept_parts.append(part)
        parts = kept_parts
        if not parts or min(len(norm(text)) for _index, text, _bbox in parts) < 20:
            out.append(block)
            continue
        origin = Counter(norm(block.text))
        split = Counter(dropped)
        for _index, text, _bbox in parts:
            split += Counter(norm(text))
        # 解析行拼接与原始 words 之间常有连字符/断词差异，严格相等会让
        # 真正可拆的混合块全部退回 preserved。改为字符级覆盖率门禁：
        # 拆出的内容必须覆盖原块 >=98%，否则视为不守恒、不拆。
        total = sum(origin.values())
        covered = sum((origin & split).values())
        if total and covered / total < 0.98:
            out.append(block)
            continue
        for index, text, bbox in parts:
            c0, c1 = columns[index]
            on_side = any(
                min(c1, band_x1) - max(c0, band_x0) > (c1 - c0) * 0.5
                for band_x0, band_x1 in bands
            )
            if not on_side:
                # 词框跨过列界时，信息栏标签会被带进正文片段；剥掉再翻译，
                # 否则中文里会出现「引用：在所有情景中……」这类夹生开头。
                text = _FRONT_LABEL_RE.sub("", text, count=1).lstrip()
                if not text:
                    continue
            # 拆出来的正文片段不能继承「单位/页脚」身份，否则会被当成不可译
            # 又留一遍英文——它本来就是被解析误并进来的正文。
            kind = block.kind
            if not on_side and kind in ("affiliation", "footnote"):
                kind = "para"
            if on_side:
                translatable = bool(block.translatable) and not _is_sidebar_whitelist(text)
            else:
                translatable = kind in ("para", "abstract") and not _is_sidebar_whitelist(text)
            child_rects = [
                tuple(value)
                for value in (block.text_rects or [])
                if int(value[0]) == block.page
                and float(value[3]) > c0 - 1
                and float(value[1]) < c1 + 1
                and float(value[2]) >= bbox[1] - 1.5
                and float(value[4]) <= bbox[3] + 1.5
            ] or [(block.page, bbox[0], bbox[1], bbox[2], bbox[3])]
            out.append(
                Block(
                    id="",
                    order=0,
                    page=block.page,
                    kind=kind,
                    text=text,
                    bbox=bbox,
                    size=block.size,
                    section=block.section,
                    translatable=translatable,
                    column_role="sidebar" if on_side else "body",
                    text_rects=child_rects,
                )
            )
    # 整块就落在信息栏列内的块（作者单位 / 期刊信息 / citation 行）同样是
    # 不可译白名单：拼版层不需要再猜，直接按 column_role 保留原文。
    for block in out:
        if block.column_role:
            continue
        bands = side_bands.get(block.page, [])
        if bands and any(
            block.bbox[0] >= band_x0 - 2 and block.bbox[2] <= band_x1 + 2
            for band_x0, band_x1 in bands
        ):
            block.column_role = "sidebar"
            # 侧栏原地翻译：只有联系方式/标识走白名单，机构与 citation 要译。
            if _is_sidebar_whitelist(block.text):
                block.translatable = False
    for index, block in enumerate(out):
        block.order = index
        block.id = f"b{index:04d}"
    return out


def _merge_body_paragraph_fragments(
    blocks: list[Block], body_size: float
) -> list[Block]:
    """把被侧栏混合块切断的同栏正文碎片并回一段。

    首页「左侧信息栏 + 右侧正文」的混合块夹在正文行中间时，解析会把一个
    段落切成若干 10pt 高的小块（bbox 只盖一行）。逐块翻译会让同一句话断成
    几截，译文读起来「东一句西一句」。这里只并「前块句子还没写完」的相邻
    同栏块：正常段落之间一定以句末标点结束，不会被误并；被切断的句子前块
    结尾通常停在单词中间（如 ``Predic``），必须续上。
    """
    if not blocks:
        return blocks
    gap_limit = max(body_size * 1.6, 8.0)
    out: list[Block] = []
    last_body = -1  # out 里最近一个「正文」块的下标，用来跨过侧栏小块续句
    for block in blocks:
        target: Block | None = None
        if block.kind in ("para", "abstract") and block.column_role != "sidebar":
            if last_body >= 0:
                cand = out[last_body]
                if (
                    cand.page == block.page
                    and cand.column_role != "sidebar"
                    # 跨页块不参与合并：它的 bbox 只盖每页首行，把另一页的
                    # 段落并进来会连页码归属一起带错。
                    and len(cand.bboxes) <= 1
                    and len(block.bboxes) <= 1
                    and not _ends_sentence(cand.text)
                    # 同栏锚用「右缘对齐」而不是左缘：拆出的正文片段常把左侧
                    # 一个标签词（Citation: 之类）带进来，左缘会偏进信息栏，
                    # 右缘仍是正文列右界。双栏左右两段的右缘相差整栏宽。
                    and abs(block.bbox[2] - cand.bbox[2]) <= 12
                    and _x_overlap(cand.bbox, block.bbox) >= 0.6
                    and -gap_limit <= block.bbox[1] - cand.bbox[3] <= gap_limit
                ):
                    target = cand
        if target is not None:
            # 合并文本会让行内公式偏移整体后移，必须同步平移，否则公式落点
            # 会跑进前面几个词里（与归一化改变长度是同一类偏移地雷）。
            shift = len(target.text) + 1
            target.text = _join_text(target.text, block.text)
            target.bboxes = _merge_bbox_lists(target.bboxes, block.bboxes)
            target.text_rects = sorted(
                list(target.text_rects) + list(block.text_rects),
                key=lambda rect: (int(rect[0]), float(rect[2]), float(rect[1])),
            )
            target.bbox = (
                # 左界取更靠两者右者：正文列左界固定，向左扩张只可能来自
                # 混进来的信息栏标签，不能让它把正文块拉进侧栏。
                max(target.bbox[0], block.bbox[0]), min(target.bbox[1], block.bbox[1]),
                max(target.bbox[2], block.bbox[2]), max(target.bbox[3], block.bbox[3]),
            )
            if block.translated:
                target.translated = _join_text(target.translated, block.translated)
            for math in block.inline_math or []:
                target.inline_math.append(
                    {
                        **math,
                        "start": int(math.get("start", 0)) + shift,
                        "end": int(math.get("end", 0)) + shift,
                    }
                )
            target.translatable = target.translatable or block.translatable
            if block.kind == "abstract":
                target.kind = "abstract"
            continue
        out.append(block)
        if block.kind in ("para", "abstract") and block.column_role != "sidebar":
            last_body = len(out) - 1
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
    return out


_JOURNAL_VOL_RE = re.compile(
    r"\bFront\.(?:\s+[A-Z][A-Za-z.]*){1,4}\s+\d+\s*[:：]\s*\d+\s*\.?",
    re.IGNORECASE,
)
_DOI_RE = re.compile(r"\bdoi:\s*10\.\S+", re.IGNORECASE)


def _strip_front_citation_noise(
    blocks: list[Block],
    side_bands: dict[int, list[tuple[float, float]]] | None = None,
) -> list[Block]:
    """剥掉首页正文块里混进来的期刊卷期与 DOI，并把被信息栏拉宽的左界收回。

    首页底部的 citation 行常与正文同一视觉行，跨列拆分对「bbox 只盖首行」
    的跨页块无能为力，citation 片段会留在正文里，译文就变成「…科学前沿
    12:717552。而环境组学组装…」。这里按文本模式剥掉，只动首页正文块；
    带行内公式的块跳过清洗（改长度会让公式偏移错位），但几何修正照做。
    """
    side_bands = side_bands or {}
    for b in blocks:
        if b.page != 1 or b.column_role == "sidebar" or b.kind not in ("para", "abstract"):
            continue
        if not b.inline_math:
            cleaned = _DOI_RE.sub(" ", _JOURNAL_VOL_RE.sub(" ", b.text))
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            if cleaned and cleaned != b.text:
                b.text = cleaned
        # 正文块若从信息栏内起笔，说明左界被并进来的侧栏文字拉宽了；把
        # 首页段的左界收到信息栏右缘，排版层才不会把它当跨列块去压侧栏。
        bands = side_bands.get(b.page, [])
        for band_x0, band_x1 in bands:
            if b.bbox[0] < band_x1 - 2 < b.bbox[2]:
                b.bbox = (band_x1 + 2, b.bbox[1], b.bbox[2], b.bbox[3])
                for i, item in enumerate(b.bboxes):
                    page_no, x0, y0, x1, y1 = item
                    if int(page_no) == 1 and x0 < band_x1 - 2 < x1:
                        b.bboxes[i] = (page_no, band_x1 + 2, y0, x1, y1)
                break
    return blocks


def _merge_front_matter_cards(
    blocks: list[Block], front: FrontMatter | None, page_width: float
) -> list[Block]:
    """把首页前置区按「作者卡片」聚合，而不是按阅读顺序逐行合并。

    三列作者卡片在阅读顺序里是 1st/2nd/3rd、4th/5th 逐带输出；如果直接
    按顺序合并同 kind 块，会把 1st 的邮箱和 4th 的姓名粘在一起。这里先按
    x 位置分列、列内按 y 与姓名行分卡，再按卡片在原序列中的首个位置放回。
    """
    if front is None or front.head_end <= 0:
        return blocks
    idx = [
        i
        for i, b in enumerate(blocks)
        if b.page == front.page
        and b.kind in ("author", "affiliation")
        and b.bbox[1] < front.head_end - 0.5
    ]
    if len(idx) < 2:
        return blocks
    items = [blocks[i] for i in idx]

    # 通栏作者行（AASTeX/ ApJ：一行排多位作者，单位另起整段）不是「多列
    # 作者卡片」。若仍按 x 聚类分卡，姓名行与下方单位行会被并进同一张卡，
    # 整段单位重新被吞进 author。这里直接交给 _merge_affiliations：
    # author 行按行合并、affiliation 行单独合并，两类永不混。
    if page_width > 0 and any(
        b.kind == "author" and (b.bbox[2] - b.bbox[0]) > page_width * 0.5
        for b in items
    ):
        return blocks

    # 一维 x 聚类：作者列心之间通常隔着明显空白，列内的姓名/单位/邮箱
    # 只是几百 pt 内的左右摆动。阈值随页宽缩放，窄页也不会把两列并成一列。
    threshold = max(50.0, page_width * 0.1)
    ordered = sorted(items, key=lambda b: (b.bbox[0] + b.bbox[2]) / 2)
    columns: list[list[Block]] = [[ordered[0]]]
    for b in ordered[1:]:
        prev = columns[-1][-1]
        prev_cx = (prev.bbox[0] + prev.bbox[2]) / 2
        cx = (b.bbox[0] + b.bbox[2]) / 2
        if cx - prev_cx > threshold:
            columns.append([b])
        else:
            columns[-1].append(b)

    line_h = max(max((b.size for b in items), default=10.0) * 1.2, 8.0)
    cards: list[list[Block]] = []
    for col in columns:
        col.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
        cur = [col[0]]
        for b in col[1:]:
            gap = b.bbox[1] - cur[-1].bbox[3]
            if _looks_author_name(b.text) or gap > max(line_h * 2.2, 8.0):
                cards.append(cur)
                cur = [b]
            else:
                cur.append(b)
        cards.append(cur)

    pos = {id(b): i for i, b in zip(idx, items)}
    first_at: dict[int, Block] = {}
    remove: set[int] = set()
    for card in cards:
        positions = sorted(pos[id(b)] for b in card)
        first_at[positions[0]] = _card_block(card)
        remove.update(positions[1:])

    out: list[Block] = []
    for i, b in enumerate(blocks):
        if i in remove:
            continue
        out.append(first_at.get(i, b))
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
    return out


def _merge_affiliations(blocks: list[Block]) -> list[Block]:
    """合并连续的单位/作者块（大型论文的作者单位列表常横跨整页）。"""
    out: list[Block] = []
    for b in blocks:
        if (
            out
            and out[-1].kind == b.kind
            and b.kind in ("affiliation", "author")
            and out[-1].page == b.page
            # 只合并同一栏里的作者/单位行；双栏首页的侧栏单位与右栏摘要
            # 横向不重叠，按「同页同 kind」合并会把两栏文字拼成一段
            and _x_overlap(out[-1].bbox, b.bbox) >= 0.3
            # 同一张卡片内行距很小；跨过一整段空白说明是下一位作者，不能并。
            and b.bbox[1] - out[-1].bbox[3] <= max(
                2.2 * max(out[-1].size, b.size, 1.0), 8.0
            )
        ):
            prev = out[-1]
            prev.text = _join_text(prev.text, b.text)
            prev.bboxes = _merge_bbox_lists(prev.bboxes, b.bboxes)
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


# 参考文献编号开头：`[12]` / `(12)` / `12.` / `12)`。方括号编号后面通常
# 只有空格，不能再强制要求 `.`/`)`，否则 `[22] Author...` 永远判不成条目。
_REF_START_RE = re.compile(r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s+\S")
_REF_YEAR_RE = re.compile(r"\b(?:19[5-9]\d|20[0-3]\d)\b")


def _looks_reference_entry(text: str) -> bool:
    """编号开头、且像文献条目（有年份/et al./DOI）而不是正文编号列表。"""
    if len(text) < 28 or not _REF_START_RE.match(text):
        return False
    return bool(
        _REF_YEAR_RE.search(text)
        or re.search(r"\bet\s+al\b|\bdoi\b|arxiv|https?://", text, re.IGNORECASE)
    )


def _classify(
    blocks: list[Block],
    body_size: float,
    page_height: float = 792.0,
    front: FrontMatter | None = None,
) -> None:
    """按位置与文本特征标注块类型，并确定「不翻译」的块。"""
    in_refs = False
    section = ""
    front_matter = True          # 仍在首页标题区（Abstract 之前）
    body_started = False         # 已进入 Introduction，之后不再有作者单位
    in_tail_meta = False         # 已进入文末作者简介/声明区
    last_page = max((b.page for b in blocks), default=1)
    title_idx = _pick_title_index(blocks, page_height)
    _merge_title_lines(blocks, title_idx)

    for i, b in enumerate(blocks):
        t = b.text.strip()

        if i == title_idx:
            b.kind = "title"
            b.level = 0
            continue

        # 图表注必须先于作者单位/标题区判定：首页的图形摘要、页面顶部的大表
        # 常让 "Figure 1 / Table 1" 落到 heading 分支，被吞成 affiliation，
        # 后续按图注配对图和表时就找不到锚点。
        if _CAPTION_RE.match(t) and _is_caption_text(t):
            b.kind = "caption"
            b.section = section
            # 图注/表注随论文一起翻译：前端图表栏优先展示配对 caption 块的
            # translated（captionZh），保持 False 会让图注永远只有英文。
            b.translatable = True
            continue

        # 已进入文末作者简介/声明区：其后的连续段落整段折叠，直到新的章节标题
        # 或参考文献条目出现。否则声明正文会被参考文献状态吞成 reference。
        if in_tail_meta:
            if (
                b.kind in ("equation", "figure", "table")
                or _looks_reference_entry(t)
                or (b.kind == "heading" and not _TAIL_META_HEAD_RE.match(t))
            ):
                in_tail_meta = False
            elif len(t) >= 20:
                b.kind = "footnote"
                b.translatable = False
                b.level = 0
                b.section = section
                continue

        # 文末/页底的作者简介、通讯方式与声明类信息：和作者单位一样折叠，
        # 不进入正文流、不翻译。放行首页作者卡片区（那里的邮箱属于作者块）。
        if (
            b.kind in ("para", "heading")
            and not (front is not None and front.page == b.page and front.in_head(b.bbox))
            and _is_tail_metadata(b, t, body_size, page_height, last_page, in_refs)
        ):
            b.kind = "footnote"
            b.translatable = False
            b.level = 0
            b.section = section
            in_tail_meta = bool(in_refs or b.page >= max(1, last_page - 1))
            continue

        # 首页辅助窄列（通讯作者/邮箱/单位）与摘要并列，属于元数据，不是正文。
        if (
            front is not None
            and front.page == b.page
            and front.in_aux(b.bbox)
            and b.kind not in ("equation", "figure", "table")
        ):
            b.kind = "affiliation"
            b.translatable = False
            b.level = 0
            continue

        # 版权/开放获取声明是 7~8pt 脚注，不是可翻译正文。
        if (
            b.kind == "para"
            and b.size < body_size * 0.9
            and re.search(
                r"©|Creative\s+Commons|All\s+rights\s+reserved|"
                r"This\s+article\s+is\s+licensed",
                t,
                re.IGNORECASE,
            )
        ):
            b.kind = "footnote"
            b.translatable = False
            continue

        # 声明区的「Not applicable.」与收稿/接受日期是元数据，不是正文。
        if b.kind == "para" and re.fullmatch(r"(?:Not applicable|N/?A)\.?", t, re.IGNORECASE):
            b.kind = "footnote"
            b.translatable = False
            continue
        if b.kind == "para" and re.match(
            r"^(?:Received|Accepted|Published)\s*[:：]", t, re.IGNORECASE
        ):
            b.kind = "footnote"
            b.translatable = False
            continue

        # 前置区（标题下方、摘要之前）是作者卡片，不是正文。用显式的
        # FrontMatter 范围判定，覆盖三列卡片 / 通栏作者行等版式；姓名行
        # 才是 author，单位、邮箱、城市等归 affiliation。
        if (
            front is not None
            and front.page == b.page
            and front.in_head(b.bbox)
            and b.kind not in ("equation", "figure", "table")
        ):
            b.kind = "author" if _looks_author_name(t) else "affiliation"
            b.translatable = False
            b.level = 0
            continue

        # 摘要/关键词在 _build_blocks 里已定 kind，这里只补 section 与翻译标记。
        if b.kind in ("abstract", "keywords"):
            b.section = "Abstract"
            b.translatable = True
            continue

        if b.kind == "heading":
            # 页码（如参考文献页顶部的 "27"）常因字号/粗体被判成 heading。
            # 它既不是章节标题，也不能结束参考文献状态——否则跨页后的
            # `[22] ...` 条目会被当成正文段，后续引用全部对不上。
            if re.fullmatch(r"\d{1,4}", t):
                b.kind = "para"
                b.translatable = False
                continue
            # 首页 Abstract 之前的大字号块是作者/单位，不是章节标题
            if (
                front_matter
                and b.page == 1
                and (front is None or front.page == 0)
                and not _is_abstract_head(t)
            ):
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
            if _is_abstract_head(t):
                front_matter = False
                # 很多期刊（如 Nature）没有 Introduction 标题；摘要之后就是正文，
                # 否则正文里的 "laboratory / Institute" 会被误判成作者单位。
                body_started = True
            if _INTRO_HEAD_RE.match(t) or _is_abstract_head(t):
                body_started = True
            continue

        b.section = section

        # 公式/图/表已在构建阶段定型，不再改判
        if b.kind in ("equation", "figure", "table"):
            continue

        # 首页/次页底部的作者脚注（单位、邮箱、通讯作者）是元数据，不翻译。
        if (
            b.page <= 2
            and b.kind == "para"
            and len(t) < 320
            and _AUTHOR_NOTE_RE.search(t)
        ):
            b.kind = "affiliation"
            b.translatable = False
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

        if front_matter and b.page == 1 and (front is None or front.page == 0):
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


_VIRTUAL_WORDS = {
    "of", "and", "the", "for", "in", "on", "to", "with", "a", "an",
    "from", "by", "as", "at", "or", "via", "vs",
}


def _title_case_short(text: str) -> bool:
    """短标题形态：实词首字母大写（或全大写），至少两个实词。

    正文句子即使很短也不会每个实词都大写，故该判据能把
    ``Baseline Additive-Dominant GBLUP`` 这类伪粗体小标题和正文分开。
    """
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", text)
    if len(words) < 2:
        return False
    strong = 0
    for word in words:
        if word.lower() in _VIRTUAL_WORDS:
            continue
        if not (word[:1].isupper() or word.isupper()):
            return False
        strong += 1
    return strong >= 2


def _promote_short_headings(blocks: list[Block], body_size: float) -> list[Block]:
    """把字号略高于正文的 Title Case 短行补判为小标题。

    Frontiers 等期刊的小节标题常用 10.0pt（正文 9.5pt）排伪粗体，PDF 字体
    信息里没有 bold 标记，字号差也够不到 ``+1.2pt`` 的门槛，于是整段落进
    ``para``：回填时按正文字号写、没有层级，读者看不出是新小节（第 7 页
    ``Baseline Additive-Dominant GBLUP`` 即此形态）。这里只补判「短、无句末
    标点、Title Case、后接正文」的块，不改字号判据以外的既有规则。
    """
    for i, block in enumerate(blocks):
        if block.kind != "para":
            continue
        text = block.text.strip()
        if not (2 <= len(text.split()) <= 12 and len(text) <= 70):
            continue
        if text.endswith((".", "?", ";", ",", ":")) or ": " in text or ". " in text:
            continue
        if not text[:1].isupper() or not _title_case_short(text):
            continue
        if block.size and body_size and block.size < body_size - 0.1:
            continue
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if nxt is None or nxt.page != block.page or nxt.kind not in (
            "para", "abstract", "heading"
        ):
            continue
        block.kind = "heading"
        block.level = 3
        block.section = text
    return blocks


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
    # 各页非编号块的右边界：编号贴着某一栏的右缘，不一定是整页右缘
    edge_xs: dict[int, list[float]] = {}
    for blk in blocks:
        if _EQ_NUM_ONLY_RE.match(blk.text.strip()):
            continue
        edge_xs.setdefault(blk.page, []).append(blk.bbox[2])
    edge_xs = {p: sorted(v) for p, v in edge_xs.items()}
    drop: set[int] = set()
    for b in blocks:
        if b.kind != "para" or not _EQ_NUM_ONLY_RE.match(b.text.strip()):
            continue
        width = widths.get(b.page, 612)
        if b.bbox[2] < width * 0.6 and not _aligns_column_edge(
            edge_xs.get(b.page, []), b.bbox[2]
        ):
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
    """丢弃页码、孤立标点、纯符号等碎片。

    目录页的引导符点线（`. . . . . . 17`）也在这里处理：整段只有点线的块直接
    丢掉，标题后面拖点线的块剥掉点线、只留标题与页码。否则这些点线会原样
    进入翻译，模型再复制一遍，阅读页就会出现整屏原点。
    """
    out = []
    for b in blocks:
        t = b.text.strip()
        if not t:
            continue
        if len(t) <= 2 and not t.isalpha():
            continue
        if re.fullmatch(r"[\d\s\.\-–—]+", t):
            continue
        if is_dot_leader_only(t):
            continue
        if b.kind not in ("equation", "figure", "table") and has_dot_leader(t):
            cleaned = strip_dot_leaders(t)
            if not cleaned or is_dot_leader_only(cleaned):
                continue
            if cleaned != b.text:
                b.text = cleaned
                # 文本被裁剪过，行内公式的字符偏移已失效，清掉避免错位渲染
                b.inline_math = []

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


def _x_overlap(a: tuple, b: tuple) -> float:
    """两块的横向重叠占较窄块宽度的比例。"""
    inter = min(a[2], b[2]) - max(a[0], b[0])
    narrow = min(a[2] - a[0], b[2] - b[0])
    return inter / narrow if narrow > 0 else 0.0


def _block_in_table_rects(
    block: "Block", table_rects: dict[int, list[pymupdf.Rect]] | None
) -> bool:
    """块的几何中心是否落在已检测到的表格区域里。"""
    if not table_rects:
        return False
    cx = (block.bbox[0] + block.bbox[2]) / 2
    cy = (block.bbox[1] + block.bbox[3]) / 2
    return any(
        rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1
        for rect in table_rects.get(block.page, [])
    )


def _merge_across_pages(
    blocks: list["Block"],
    table_rects: dict[int, list[pymupdf.Rect]] | None = None,
) -> list["Block"]:
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
        # 表格区里的短行属于表格数据，不能按「没说完的段落」与旁边正文合并；
        # 否则表 2 这类多行表会被并成几大块，后面按表题切表就找不到行块。
        if (
            b.kind in ("para", "caption")
            and not _ends_sentence(b.text)
            and not _block_in_table_rects(b, table_rects)
        ):
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
                    and not _block_in_table_rects(n, table_rects)
                    # 英文看小写开头，中文没有大小写，汉字开头即可作为接续
                    # 证据（上一块已被 `_ends_sentence` 判定为没说完）。
                    and (n.text[:1].islower() or bool(_CJK_RE.match(n.text[:1])))
                    and not _CANNOT_CONTINUE_RE.match(n.text)
                    # 同页续段只发生在同一栏（被通栏图表劈开）；左右栏的
                    # x 范围不重叠，不能因为「上一块没收句 + 下一块小写开头」
                    # 就把两栏正文拼成一段
                    and (n.page != b.page or _x_overlap(b.bbox, n.bbox) >= 0.3)
                    # 宽度差得太多说明两块不是同一段：图注比正文窄，图内的小标注
                    # （「(b) 三维模型可视化」）也比正文窄。图注要求更严格。
                    and (
                        min(b.bbox[2] - b.bbox[0], n.bbox[2] - n.bbox[0])
                        >= (0.6 if b.kind == "caption" else 0.35)
                        * max(b.bbox[2] - b.bbox[0], n.bbox[2] - n.bbox[0], 1.0)
                    )
                ):
                    b.text, offset = _append_line_text(b.text, n.text)
                    lead = len(n.text) - len(n.text.lstrip())
                    for item in n.inline_math:
                        start_pos = int(item["start"]) - lead + offset
                        end_pos = int(item["end"]) - lead + offset
                        if start_pos < 0 or end_pos <= start_pos:
                            continue
                        b.inline_math.append(
                            {**item, "start": start_pos, "end": end_pos}
                        )
                    b.bboxes = _merge_bbox_lists(b.bboxes, n.bboxes)
                    # 跨页续段无法用一个矩形表示；保留起始页的 bbox，
                    # 避免并集成整页/两页导致图表与公式的几何判断失效
                    out.extend(skipped)     # 图表等保持原位，接在合并后的段落之后
                    i = j + 1
                    continue
        i += 1
    return out


def _merge_front_continuations(blocks: list["Block"]) -> list["Block"]:
    """把跨页/续行的摘要与关键词并回同一条目。

    `_classify` 只按首页标记给第一段定 kind；摘要续页与关键词末行会被当成
    普通 para。它们既不该单独翻译，也不该在阅读页里落在 Keywords 之后。
    只吸收紧邻、以接续字符开头、且当前条目尚未结束的行，遇到下一个小节
    或图表立即停止。
    """
    out: list["Block"] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind not in ("abstract", "keywords"):
            out.append(b)
            i += 1
            continue
        j = i + 1
        skipped: list["Block"] = []
        while j < len(blocks) and not _ends_sentence(b.text):
            n = blocks[j]
            # 首页作者脚注（邮箱/通讯作者）可能夹在摘要与续页之间；
            # 它不是正文，但不能因此切断摘要跨页合并。跳过并保留原位。
            if n.kind in ("affiliation", "author", "footnote"):
                skipped.append(n)
                j += 1
                continue
            if n.kind != "para" or n.page not in (b.page, b.page + 1):
                break
            if b.kind == "keywords" and len(n.text.strip()) > 160:
                break
            if not (_CONTINUE_START.match(n.text) or n.text[:1] in "(["):
                break
            text = n.text
            b.text, offset = _append_line_text(b.text, text)
            lead = len(text) - len(text.lstrip())
            for item in n.inline_math:
                start_pos = int(item["start"]) - lead + offset
                end_pos = int(item["end"]) - lead + offset
                if start_pos < 0 or end_pos <= start_pos:
                    continue
                b.inline_math.append({**item, "start": start_pos, "end": end_pos})
            b.bboxes = _merge_bbox_lists(b.bboxes, n.bboxes)
            j += 1
        out.append(b)
        out.extend(skipped)
        i = j
    for idx, b in enumerate(out):
        b.order = idx
        b.id = f"b{idx:04d}"
    return out


def _merge_heading_continuations(blocks: list["Block"]) -> list["Block"]:
    """把被排版拆成两行的正文章节标题并回一条。

    AASTeX / CVPR 的章节标题常与正文同字号、正体，第二行会被当成 para 或
    另一个 heading（`3 FEW-SHOT MOLECULAR PROPERTY PREDIC-` + `TION METHODS`）。
    只对编号标题吸收紧邻、同字号、短且未以句读结尾的下一块；无编号的粗体
    短句不能吸收 para，否则正文首行会被并进标题。
    """
    numbered_head = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[A-Z]\.)\s+")
    out: list["Block"] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind != "heading":
            out.append(b)
            i += 1
            continue
        while i + 1 < len(blocks):
            n = blocks[i + 1]
            if n.page != b.page or n.kind not in ("heading", "para"):
                break
            if b.text.rstrip().endswith((".", "?", "!", ";", ":")):
                break
            if len(n.text.strip()) > 90:
                break
            if n.text.strip().endswith((".", "?", "!", ";", ":")):
                break
            if _is_caption_text(n.text):
                break
            # 只有编号标题才允许吸收普通 para；无编号粗体短句很可能只是
            # 正文段首，不能把整段第一行并进标题。
            if n.kind == "para" and not numbered_head.match(b.text):
                break
            if n.kind == "heading" and numbered_head.match(n.text):
                break
            if n.kind == "heading":
                # 全大写 section 后接 Title Case 标题（`MATERIALS AND METHODS`
                # + `Ethics Statement`）是两个独立标题，不能并成一条。
                if b.text.isupper() and not n.text.isupper():
                    break
                n_words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", n.text)
                # 前块以单个大写字母收尾（`... From the W` + `Matrix (W-GP)`）
                # 说明标题还没写完，下一行必须并回来；否则会拆成两个标题。
                last_word = (b.text.split() or [""])[-1]
                dangling = bool(re.fullmatch(r"[A-Z]", last_word)) or b.text.rstrip().endswith("-")
                if (
                    len(n_words) >= 2
                    and not n.text[:1].islower()
                    and not dangling
                ):
                    break
            if b.size and n.size and abs(b.size - n.size) > 1.2:
                break
            gap = n.bbox[1] - b.bbox[3]
            if gap > max((b.size or n.size or 10.0) * 1.25, 4.0):
                break
            if abs(n.bbox[0] - b.bbox[0]) > 30 and abs(n.bbox[2] - b.bbox[2]) > 30:
                break
            b.text = _join_text(b.text, n.text)
            b.bboxes = _merge_bbox_lists(b.bboxes, n.bboxes)
            i += 1
        out.append(b)
        i += 1
    for idx, x in enumerate(out):
        x.order = idx
        x.id = f"b{idx:04d}"
    return out


_ROTATED_CAPTION_RE = re.compile(
    r"^\s*((?:Supplementary\s+|Extended\s+Data\s+)?(?:Figure|Fig\.|Table|Tab\.|FIGURE|TABLE))"
    r"\s*([A-Z]?\d+)",
    re.IGNORECASE,
)


def _collect_rotated_caption_blocks(doc: pymupdf.Document) -> list[Block]:
    """收集旋转 90° 的图注/表注（横排大表常整页旋转放置）。

    `_collect_lines` 会过滤旋转文字，所以 Table 3 这类旋转表的标题不会进入
    正常图注流；表格虽然被 find_tables 找到，却因为没有可配对的 caption
    被整张丢掉，随后同页矢量图兜底又把表格误裁成 figure。这里只收旋转的
    「Figure/Table + 编号」行，不参与正文，供表格/图注配对使用。
    """
    grouped: dict[tuple[int, str, str], Block] = {}
    for pno in range(len(doc)):
        page = doc[pno]
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines") or []:
                direction = line.get("dir") or (1, 0)
                if abs(float(direction[1])) <= 0.15:
                    continue
                spans = [
                    s for s in line.get("spans") or []
                    if (s.get("text") or "").strip()
                ]
                if not spans:
                    continue
                text = _normalize_text(_join_spans(spans)).strip()
                if not text or not _CAPTION_RE.match(text) or not _is_caption_text(text):
                    continue
                m = _ROTATED_CAPTION_RE.match(text)
                if not m:
                    continue
                key = (pno + 1, m.group(1).lower(), m.group(2).upper())
                bbox = tuple(line["bbox"])
                size = median(
                    [float(s.get("size") or 0) for s in spans]
                ) or 10.0
                if key in grouped:
                    cap = grouped[key]
                    cap.text = _join_text(cap.text, text)
                    cap.bbox = (
                        min(cap.bbox[0], bbox[0]), min(cap.bbox[1], bbox[1]),
                        max(cap.bbox[2], bbox[2]), max(cap.bbox[3], bbox[3]),
                    )
                    cap.size = max(cap.size, size)
                else:
                    grouped[key] = Block(
                        id="", order=0, page=pno + 1, kind="caption",
                        text=text, bbox=bbox, size=size, translatable=True,
                    )
    return list(grouped.values())


def _insert_rotated_captions(blocks: list[Block], captions: list[Block]) -> list[Block]:
    """把旋转图注插到所属页的开头；后续表格抽取会把它作为锚点。

    旋转表通常独占一页，页首插入不会打乱正文；若同页已有相同文本的
    正常图注，则不重复插入。
    """
    if not captions:
        return blocks
    out = list(blocks)
    for cap in captions:
        if any(
            b.kind == "caption" and b.page == cap.page
            and b.text.strip() == cap.text.strip()
            for b in out
        ):
            continue
        idx = next((i for i, b in enumerate(out) if b.page >= cap.page), len(out))
        out.insert(idx, cap)
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
    return out


def _merge_small_caption_continuations(blocks: list[Block], body_size: float) -> list[Block]:
    """把图注之后紧邻的小字号续行并回 caption。

    图注末行常被拆成独立的 8pt 小字（`... in silico` + `experiment`），
    它们不是正文；图注本身又没有句末标点，单靠 `_continues_paragraph`
    容易被字号/行距判据挡住。这里只接受「同页、紧邻、小字号、短行」。
    """
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind != "caption" or _is_table_caption(b.text):
            out.append(b)
            i += 1
            continue
        j = i + 1
        while j < len(blocks):
            n = blocks[j]
            if n.page != b.page or n.kind != "para":
                break
            if body_size > 0 and n.size >= body_size * 0.92:
                break
            if len(n.text.strip()) > 80:
                break
            gap = n.bbox[1] - b.bbox[3]
            if gap < -2 or gap > max(body_size * 1.8, 14.0):
                break
            overlap = min(b.bbox[2], n.bbox[2]) - max(b.bbox[0], n.bbox[0])
            if overlap < 0.3 * min(b.bbox[2] - b.bbox[0], n.bbox[2] - n.bbox[0]):
                break
            b.text = _join_text(b.text, n.text)
            b.bboxes = _merge_bbox_lists(b.bboxes, n.bboxes)
            j += 1
        out.append(b)
        i = j
    for idx, b in enumerate(out):
        b.order = idx
        b.id = f"b{idx:04d}"
    return out


def _absorb_table_notes(blocks: list[Block], body_size: float) -> list[Block]:
    """把表格下方紧邻的小字号表注并回表题。

    BMC/医学期刊的表注常排在表格下方 7pt 小字（`— Represents ...`、
    疾病缩写列表），既不是正文也不是独立图注。留在正文流会破坏段落，
    翻译也会把它们当正文；并回 caption 后随表格一起展示。
    """
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind != "table":
            out.append(b)
            i += 1
            continue
        cap = next(
            (
                c for c in blocks
                if c.page == b.page and c.kind == "caption"
                and _is_table_caption(c.text)
                and _rect_gap(pymupdf.Rect(c.bbox), pymupdf.Rect(b.bbox)) <= 80
            ),
            None,
        )
        j = i + 1
        notes: list[Block] = []
        while j < len(blocks):
            n = blocks[j]
            if n.page != b.page or n.kind != "para":
                break
            gap = n.bbox[1] - b.bbox[3]
            if gap < -2 or gap > max(body_size * 1.8, 14.0):
                break
            if body_size > 0 and n.size >= body_size * 0.92:
                break
            if len(n.text.strip()) > 300:
                break
            overlap = min(b.bbox[2], n.bbox[2]) - max(b.bbox[0], n.bbox[0])
            if overlap < 0.3 * min(b.bbox[2] - b.bbox[0], n.bbox[2] - n.bbox[0]):
                break
            notes.append(n)
            j += 1
        if notes:
            note_text = " ".join(n.text.strip() for n in notes)
            base = b.caption or (cap.text if cap is not None else "")
            b.caption = _join_text(base, note_text)
            if cap is not None:
                cap.text = _join_text(cap.text, note_text)
            out.append(b)
            i = j
            continue
        out.append(b)
        i += 1
    for idx, b in enumerate(out):
        b.order = idx
        b.id = f"b{idx:04d}"
    return out


def _table_text_rotation(page: pymupdf.Page, rect: pymupdf.Rect) -> int:
    """表格内文字的整体旋转角度；0 表示正常，90/180/270 表示需转正。

    按字符数加权，避免个别横排表头把整张旋转表的判断带偏。
    """
    scores: Counter[tuple[float, float]] = Counter()
    try:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines") or []:
                direction = line.get("dir") or (1, 0)
                if abs(float(direction[1])) <= 0.15:
                    continue
                if not pymupdf.Rect(line["bbox"]).intersects(rect):
                    continue
                text = "".join(
                    (s.get("text") or "") for s in line.get("spans") or []
                )
                if text.strip():
                    scores[(round(float(direction[0]), 2), round(float(direction[1]), 2))] += len(text)
    except Exception:
        return 0
    if not scores:
        return 0
    (dx, dy), _ = scores.most_common(1)[0]
    # 文字方向 (0,-1) 表示原表逆时针旋转，裁图后需顺时针转 90° 才正立；
    # (0,1) 则相反。这里用字符数加权，不依赖单行表头。
    if dy < -0.5:
        return 90
    if dy > 0.5:
        return 270
    if dx < -0.5:
        return 180
    return 0


def _is_table_caption(text: str) -> bool:
    return bool(_TABLE_CAP_RE.match(text.strip())) and _is_caption_text(text)


def _merge_caption_continuations(
    blocks: list["Block"],
    table_rects: dict[int, list[pymupdf.Rect]] | None = None,
) -> list["Block"]:
    """把紧贴图注的续行并回 caption。

    `_classify` 只按第一行识别图注，后面几行常被拆成独立 para：图注不完整、
    续行还会被当成正文，反过来影响「按图注裁图」的区域判断。普通图注要求
    同页、紧贴（≤8pt）、左边界一致（±14pt）；只有编号的表注（`TABLE I`）
    例外——它的标题通常另起一行居中，左边界与编号不同，若不并回来，
    表格标题就只剩一个编号，标题行还可能被误判成参考文献。
    """
    out: list[Block] = []
    consumed: set[str] = set()

    # ① 同一行的图注片段：`Fig. 1.` 与标题常被排成同一行、拆成两个块，
    #    标题还可能排在编号前面（阅读顺序按栏/块排序）。这类只在纯编号
    #    图注上处理，普通图注不横向吞并，避免把同栏正文并进来。
    for b in blocks:
        if b.kind != "caption" or b.id in consumed:
            continue
        if not _CAPTION_NUM_ONLY_RE.match(b.text.strip()):
            continue
        parts = [(b.bbox[0], b.text)]
        for n in blocks:
            if n is b or n.id in consumed or n.page != b.page:
                continue
            if n.kind not in ("para", "reference", "heading"):
                continue
            # 表格区里的短行是表数据，不是图注/表注的续行
            if _block_in_table_rects(n, table_rects):
                continue
            if len(n.text.strip()) > 400:
                continue
            same_size = (not b.size or not n.size) or abs(b.size - n.size) <= 1.5
            if not same_size:
                continue
            v_overlap = min(b.bbox[3], n.bbox[3]) - max(b.bbox[1], n.bbox[1])
            if v_overlap < 0.5 * min(b.bbox[3] - b.bbox[1], n.bbox[3] - n.bbox[1]):
                continue
            h_gap = max(n.bbox[0] - b.bbox[2], b.bbox[0] - n.bbox[2], 0.0)
            if h_gap > 12:
                continue
            parts.append((n.bbox[0], n.text))
            consumed.add(n.id)
            b.bboxes = _merge_bbox_lists(b.bboxes, n.bboxes)
            b.bbox = (
                min(b.bbox[0], n.bbox[0]), min(b.bbox[1], n.bbox[1]),
                max(b.bbox[2], n.bbox[2]), max(b.bbox[3], n.bbox[3]),
            )
        if len(parts) > 1:
            parts.sort(key=lambda item: item[0])
            joined = ""
            for _, t in parts:
                joined, _ = _append_line_text(joined, t)
            b.text = joined.strip()

    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.id in consumed:
            i += 1
            continue
        if b.kind != "caption":
            out.append(b)
            i += 1
            continue
        num_only = bool(_CAPTION_NUM_ONLY_RE.match(b.text.strip()))
        base_x0 = b.bbox[0]
        last_bbox = b.bbox
        j = i + 1
        while j < len(blocks):
            n = blocks[j]
            if n.id in consumed:
                j += 1
                continue
            if n.kind not in ("para", "reference", "heading") or n.page != b.page:
                break
            # 表格区里的短行属于表格数据，不能被并进表题
            if _block_in_table_rects(n, table_rects):
                break
            gap = n.bbox[1] - b.bbox[3]
            if gap > (10 if num_only else 8) or len(b.text) > 800 or len(n.text) > 400:
                break
            # 中文图注/表注的第一行通常就是完整标题，紧跟的长正文段不能
            # 并进 caption（否则正文会被吞掉、也不再参与翻译）。
            if _CJK_RE.search(b.text) and len(n.text.strip()) > 60:
                break
            same_size = (not b.size or not n.size) or abs(b.size - n.size) <= 1.5
            # 算法框的题注只取标题行：`Input:`/`Output:`/编号行属于伪代码本体。
            # 并进 caption 会让表头变成一大段代码，算法体也会从正文里消失。
            if _ALG_CAPTION_RE.match(b.text) and (
                len(b.text) >= 120 or _ALG_BODY_LINE_RE.match(n.text.strip())
            ):
                break
            if n.kind == "heading":
                # 纯编号表注后的居中大标题可能被判成 heading；
                # 真章节标题通常更长或字号更大，用长度 + 字号挡住。
                if not num_only or len(n.text.strip()) > 120 or not same_size:
                    break
            elif not num_only and (
                abs(n.bbox[0] - base_x0) > 14 or not same_size
            ):
                # 图注之后紧贴的正文常常左边界一致、行距也紧，但字号不同
                # （图注 8pt、正文 10pt）。字号是区分「续行」与「正文」的
                # 可靠信号，必须在普通图注这里一起检查。
                break
            # 普通图注按「上一行是否写满」判断段落是否结束。只靠紧贴/左边界
            # 会把图注下方紧挨着的正文一起吞掉（实测 Fig. 4 后两段正文被并
            # 进 caption）；而按句号判断又会把图注里正常的大写续句截断
            # （"… Hallmark / enrichment analysis …"）。写满才是可靠的续行信号。
            if not num_only:
                span = max(b.bbox[2] - b.bbox[0], 1.0)
                if (last_bbox[2] - last_bbox[0]) < 0.72 * span:
                    break
            b.text = _join_text(b.text, n.text)
            b.bboxes = _merge_bbox_lists(b.bboxes, n.bboxes)
            b.bbox = (
                min(b.bbox[0], n.bbox[0]), min(b.bbox[1], n.bbox[1]),
                max(b.bbox[2], n.bbox[2]), max(b.bbox[3], n.bbox[3]),
            )
            last_bbox = n.bbox
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


def _cover_ratio(inner: pymupdf.Rect, outer: pymupdf.Rect) -> float:
    """inner 被 outer 覆盖的面积比例（0~1）。"""
    if inner.width * inner.height <= 0:
        return 0.0
    inter = inner & outer
    if inter.is_empty:
        return 0.0
    return inter.width * inter.height / (inner.width * inner.height)


def _table_cell_text(page: pymupdf.Page, bbox, raw_blocks=None) -> str:
    """Read characters in geometric order, keeping punctuation on its visual row."""
    rect = pymupdf.Rect(bbox)
    chars = []
    for block in raw_blocks if raw_blocks is not None else page.get_text("rawdict", clip=rect)["blocks"]:
        if not pymupdf.Rect(block["bbox"]).intersects(rect):
            continue
        for line in block.get("lines", []):
            if not pymupdf.Rect(line["bbox"]).intersects(rect):
                continue
            for span in line["spans"]:
                for char in span["chars"]:
                    c = char["c"]
                    box = pymupdf.Rect(char["bbox"])
                    if c.strip() and rect.contains(pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)):
                        chars.append((box, c))
    rows = []
    for box, c in sorted(chars, key=lambda item: (item[0].y0, item[0].x0)):
        cy = (box.y0 + box.y1) / 2
        match = next((row for row in rows if abs(row[0] - cy) < max(3, box.height * 0.45)), None)
        if match is None:
            match = [cy, []]
            rows.append(match)
        match[1].append((box, c))
    result = []
    for _, row in sorted(rows, key=lambda r: r[0]):
        text = ''
        previous = None
        for box, c in sorted(row, key=lambda item: item[0].x0):
            if previous is not None and box.x0 - previous.x1 > max(1.5, previous.height * .23):
                text += ' '
            text += c
            previous = box
        result.append(text)
    return '\n'.join(result)


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
            # Reconstruct text from character positions: PDF extraction order can
            # place a decimal point after all digits despite its visual position.
            # Keep the detector's cell geometry and preserve its empty cells.
            if html.startswith("<table") and hasattr(tab, "rows"):
                originals = tab.extract()
                raw_blocks = page.get_text("rawdict", clip=rect)["blocks"]
                second = [str(v).strip() for v in originals[1] if v and str(v).strip()] if len(originals) > 1 else []
                multi_header = (len(second) >= 2 and any(not v for v in originals[1])
                                and all(re.search(r"[A-Za-z]", v) for v in second))
                rendered = []
                for row_index, row in enumerate(tab.rows):
                    cells = []
                    for col_index, cell in enumerate(row.cells):
                        original = (originals[row_index][col_index] or "") if row_index < len(originals) and col_index < len(originals[row_index]) else ""
                        value = _table_cell_text(page, cell, raw_blocks) if cell else ""
                        if not value:
                            value = original
                        tag = "th" if row_index == 0 or (row_index == 1 and multi_header) else "td"
                        cells.append(f"<{tag}>{html_lib.escape(value).replace(chr(10), '<br/>')}</{tag}>")
                    rendered.append("<tr>" + "".join(cells) + "</tr>")
                if rendered:
                    html = "<table>" + "".join(rendered) + "</table>"
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
        # text 策略始终补跑：同一页可能既有带框表，也有只靠空白对齐的无框表。
        # 没有表注的误报会在 _extract_tables_structured 里被丢掉，不会污染结果。
        for item in _page_tables(page, "text"):
            if any(_overlap_ratio(item[0], r0) > 0.6 for r0, _, _ in tables):
                continue
            tables.append(item)
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


def _shrink_table_rect_off_figures(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    blocks: list[Block],
    page_no: int,
) -> pymupdf.Rect:
    """表框若把相邻图区吞进来，收缩到表格本体。

    find_tables 常把表格下方紧贴的图注/子图标题当成表格行（稀疏图论文的
    Figure 2 就是这样被整块并入 Table 2），于是 ``_extract_figures`` 会把
    图区当成「与表格重叠」直接跳过，整张图既没进图表栏、又被裁进表格图。
    这里以「独立位图区域 + 紧邻的非表格图注」为锚，把图区从表框纵向剔除。
    """
    fig_caps = [
        pymupdf.Rect(b.bbox) for b in blocks
        if b.page == page_no and b.kind == "caption" and not _is_table_caption(b.text)
    ]
    if not fig_caps:
        return rect
    try:
        infos = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
    except Exception:
        return rect
    regions = _merge_figure_regions(
        [r for r in infos if r.width * r.height > 500 and r.height > 10],
        gap_x=30.0,
        gap_y=30.0,
    )
    out = pymupdf.Rect(rect)
    for r in regions:
        if r.width * r.height <= 8000 or r.height <= 40:
            continue
        inter = r & out
        if inter.is_empty or inter.height < r.height * 0.8:
            continue
        if r.height < out.height * 0.2:
            continue
        if not any(
            -80 <= c.y0 - r.y1 <= 200 or -30 <= r.y0 - c.y1 <= 150
            for c in fig_caps
        ):
            continue
        # 图在下半部就截掉表底，在上半部就抬表顶；不把表切成两段
        if (r.y0 + r.y1) / 2 >= (out.y0 + out.y1) / 2:
            if r.y0 - out.y0 > out.height * 0.15:
                out.y1 = min(out.y1, r.y0 - 6)
        elif out.y1 - r.y1 > out.height * 0.15:
            out.y0 = max(out.y0, r.y1 + 6)
    if out.is_empty or out.height < 18 or out.width < 60:
        return rect
    return out


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
                # 表格外的成段正文不能靠「水平只差 20pt」吸进来：双栏页
                # 的 gutter 常只有 18~24pt，旧阈值会把整栏正文裁进表格图。
                # 只吸收短表格文字/表头，长句一律跳过。
                line_text = "".join(
                    (s.get("text") or "") for s in ln.get("spans") or []
                ).strip()
                if len(line_text) > 80:
                    continue
                # 只吸收与识别框水平紧邻的单元格文字。双栏页里另一栏
                # 正文与表格隔着 gutter（约 27pt），按纵向范围无条件
                # 扩边会把整页正文裁进表格图，导致图表内容错位。
                gap = max(rect.x0 - br.x1, br.x0 - rect.x1, 0.0)
                if gap > min(_TABLE_CROP_GAP, 16.0):
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
    """无框表格策略会把成段正文当成 1~2 行的表，靠行数/单元格长度区分。

    单列表格（问答列表、条目清单）是合法结构，不能因为只有一列就丢弃；
    只有在「行数少且单元格很长」时才按成段正文处理。
    """
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", html or "", re.S)
    if not cells:
        return False
    # 旋转表在 find_tables 里可能被压成 1 行 × N 列（整张表就是一排单元格），
    # 但多列表格的结构证据已经足够强，不能按「行数少 + 单元格长」判正文。
    if cols >= 4:
        return False
    rows = html.count("<tr")
    text = " ".join(re.sub(r"<[^>]+>", " ", c) for c in cells)
    text = re.sub(r"\s+", " ", text).strip()
    if rows > 2 or not text:
        return False
    avg = len(text) / len(cells)
    if cols < 2:
        return avg > 40
    return avg > 25


def _table_rect_covers_body_text(
    lines: list[Line],
    page_no: int,
    rect: pymupdf.Rect,
    page_height: float,
    page_area: float,
) -> bool:
    """整页大框是否其实是正文被 find_tables 误判成表。

    发布包缺少 ``pymupdf.layout`` 时 find_tables 会退回旧算法，把整页正文
    框成一张覆盖一半以上页面的「表」；建块阶段对表格区逐行成块，于是整页
    段落被拆成一行一块（表现为图注之后所有文字一行一段）。合法整页大表以
    短单元格行为主，正文页以长句行为主，用长句行的数量与占比区分，避免把
    真正的整页表格误杀。
    """
    big = (
        rect.height >= page_height * 0.5
        or rect.width * rect.height >= page_area * 0.45
    )
    if not big:
        return False
    inside = [
        ln for ln in lines
        if ln.page == page_no
        and not ln.is_margin
        and rect.x0 - 2 <= ln.cx <= rect.x1 + 2
        and rect.y0 - 2 <= (ln.y0 + ln.y1) / 2 <= rect.y1 + 2
    ]
    if len(inside) < 8:
        return False
    # 表格特征：过半是短单元格行，不按正文处理
    short = sum(1 for ln in inside if len(ln.text.strip()) < 40)
    if short >= len(inside) * 0.5:
        return False
    prose = [
        ln for ln in inside
        if len(ln.text.strip()) >= 80 and not _looks_like_data_row(ln.text)
    ]
    chars = sum(len(ln.text.strip()) for ln in inside)
    return len(prose) >= 6 and len(prose) >= len(inside) * 0.4 and chars >= 600


def _table_swallows_page(page_no: int, rect: pymupdf.Rect, blocks: list[Block]) -> bool:
    """表格区域若覆盖本页大部分正文，说明是 find_tables 把正文误判成表。

    注意「整页就是一张大表」与「正文被误判成表」在覆盖率上完全相同，
    只能用块结构区分：表内是一行行短条目，正文是少数几个长段落。
    只要表区里短行多、长段落少，就按合法大表放过。
    """
    covered = 0
    total = 0
    inside: list[Block] = []
    for b in blocks:
        if b.page != page_no or b.kind not in ("para", "heading"):
            continue
        n = len((b.text or "").strip())
        total += n
        cx = (b.bbox[0] + b.bbox[2]) / 2
        cy = (b.bbox[1] + b.bbox[3]) / 2
        if rect.x0 - 2 <= cx <= rect.x1 + 2 and rect.y0 - 2 <= cy <= rect.y1 + 2:
            covered += n
            inside.append(b)
    # 中文一页正文通常只有六七百字，按 800 字判会让「整页被当成续表」
    # 直接漏过去（结论段、参考文献整页消失就是这样发生的），中文按 350 字。
    if total < 350 or covered < total * 0.6:
        return False

    short = sum(
        1 for b in inside
        if len(b.text.strip()) < 160 and (b.bbox[3] - b.bbox[1]) < 45
    )
    # 表格单元格里也可能有长文字（多篇引用挤在一个格子里）；正文段落
    # 会横跨整栏，单元格长文本只有一列宽。只按长度判正文会把整页大表
    # 误杀（Table 14 的两个长单元格曾让它判定成「正文被当表」）。
    wide = rect.width * 0.65
    long_paras = sum(
        1 for b in inside
        if (b.bbox[2] - b.bbox[0]) >= wide
        and (
            len(b.text.strip()) >= 200
            # 中文一段 120 字已算长段落，但必须两行以上：表格单元格里的
            # 长题干只有一行，按长度硬判会把整页合法大表误杀。
            or (
                len(b.text.strip()) >= 120
                and (b.bbox[3] - b.bbox[1]) > max(b.size, 1) * 1.8
            )
        )
    )
    if short >= 3 and long_paras <= 1:
        return False
    return True


def _rect_has_figure_caption(
    rect: pymupdf.Rect,
    blocks: list[Block],
    page_no: int,
) -> bool:
    """表框内（或紧邻上下方）是否有非表格图注。"""
    for b in blocks:
        if b.page != page_no or b.kind != "caption" or _is_table_caption(b.text):
            continue
        cy = (b.bbox[1] + b.bbox[3]) / 2
        if rect.y0 - 2 <= cy <= rect.y1 + 2:
            return True
    return False


def _rect_is_figure_region(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    blocks: list[Block],
    page_no: int,
) -> bool:
    """判断表框其实是图区，而不是上一页表格的续表。

    跨页续表只看「本页顶部有大块表区」不够：图文卡片从页顶开始时会被
    误当续表，整张图随后被「与表格重叠」丢掉（Beyond Accuracy 的
    Figure 1 就是这样消失的）。判据要求同时满足：框旁有非表格图注，
    且框内被成块图形（矢量簇/位图）覆盖大半。真正的表格页即使紧邻
    图注，框内也不会被一个独立图形簇占满。
    """
    fig_caps = [
        pymupdf.Rect(b.bbox) for b in blocks
        if b.page == page_no and b.kind == "caption" and not _is_table_caption(b.text)
    ]
    near = [
        c for c in fig_caps
        if (min(rect.x1, c.x1) - max(rect.x0, c.x0)) > min(rect.width, c.width) * 0.3
        and (
            -30 <= c.y0 - rect.y1 <= 90
            or -30 <= rect.y0 - c.y1 <= 90
            or rect.y0 <= (c.y0 + c.y1) / 2 <= rect.y1
        )
    ]
    if not near:
        return False
    try:
        clusters = [pymupdf.Rect(r) for r in page.cluster_drawings()]
    except Exception:
        clusters = []
    try:
        images = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
    except Exception:
        images = []
    box_area = rect.width * rect.height
    for r in [*clusters, *images]:
        if r.width * r.height <= 4000 or r.height <= 40:
            continue
        inter = r & rect
        if inter.is_empty:
            continue
        if inter.width * inter.height >= box_area * 0.4:
            return True
    return False


def _trim_table_crop_off_body(
    crop: pymupdf.Rect,
    raw: pymupdf.Rect,
    blocks: list[Block],
    page_no: int,
) -> pymupdf.Rect:
    """表格裁图扩边后若把正文扫进来，按正文块把裁图收回。

    双栏页里 find_tables 只认出左栏表格，但图注/横线扩边可能把右栏正文
    一起框进来；这样表格块会覆盖正文，前端看起来就是「正文被裁进表格」。
    只裁掉确实不在原始表框内、且是长段正文/标题的块。
    """
    out = pymupdf.Rect(crop)
    for b in blocks:
        if b.page != page_no or b.kind not in ("para", "heading"):
            continue
        text = (b.text or "").strip()
        if b.kind != "heading" and len(text) < 80:
            continue
        br = pymupdf.Rect(b.bbox)
        if _cover_ratio(br, raw) > 0.4:
            continue
        if _cover_ratio(br, out) < 0.5:
            continue
        if br.x0 >= raw.x1 - 2:
            out.x1 = min(out.x1, br.x0 - 2)
        elif br.x1 <= raw.x0 + 2:
            out.x0 = max(out.x0, br.x1 + 2)
        elif br.y1 <= raw.y0 + 2:
            out.y0 = max(out.y0, br.y1 + 2)
        elif br.y0 >= raw.y1 - 2:
            out.y1 = min(out.y1, br.y0 - 2)
    out |= raw
    return out


def _extract_tables_structured(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
    detected: dict[int, list[tuple[pymupdf.Rect, str, int]]] | None = None,
) -> list[Block]:
    """优先用 find_tables 的结果：结构化 HTML + 裁剪图双份，并清掉表格区域的散碎文本。"""
    if detected is None:
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
            rect = _shrink_table_rect_off_figures(page, rect, blocks, page_no)
            cap: Block | None = None
            best_d = 1e9
            for c in captions:
                if c.id in used:
                    continue
                # 正常图注在表格上下方，旋转表注在表格左右侧；统一用矩形
                # 最近距离，不能只看 y 距离（否则竖排表注永远配不上）。
                dist = min(
                    _rect_gap(pymupdf.Rect(c.bbox), rect),
                    abs(c.bbox[1] - rect.y1),
                    abs(rect.y0 - c.bbox[3]),
                )
                if dist < best_d and dist <= 350:
                    cap, best_d = c, dist

            continued = False
            if cap is None and last_cap is not None:
                # 跨页续表：本页顶部就是表格，且上一页有表题 → 沿用上一页表题。
                # 框里若含 Figure 图注，说明是本页自己的图，不是续表。
                lp, last = last_cap
                if (
                    page_no - lp <= 1
                    and rect.y0 <= page.rect.height * 0.30
                    and not _rect_has_figure_caption(rect, blocks, page_no)
                    and not _rect_is_figure_region(page, rect, blocks, page_no)
                ):
                    cap, continued = last, True
            if cap is None:
                continue
            if _table_swallows_page(page_no, rect, blocks):
                continue
            if not continued:
                used.add(cap.id)
                last_cap = (page_no, cap)

            crop = _include_caption(_table_crop_rect(page, rect), cap.bbox, page)
            crop = _trim_table_crop_off_body(crop, rect, blocks, page_no)
            img = _crop(
                page, crop, out_dir / f"p{page_no:03d}_tab{k}.png",
                target_px=TABLE_TARGET_PX,
                rotate=_table_text_rotation(page, rect),
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
    blocks: list[Block], start: int, step: int, cap: Block,
    max_len: int = 240, max_h: float = 60.0,
) -> list[int]:
    """从表题开始，向一个方向收集连续的「表格行」短块。

    表格行在版面里就是一行行短文本：高度小、间距紧、没有句末标点。
    碰到长段落、图表、另一个表题或间距明显变大就停止。
    问答式示例表（题干 2~4 行 + 选项）单块会超过常规上限，由调用方放宽。
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
        if h > max_h or len(p.text.strip()) > max_len:
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

        up = _collect_table_rows(blocks, i - 1, -1, b, max_len=520, max_h=80)
        down = _collect_table_rows(blocks, i + 1, 1, b, max_len=520, max_h=80)
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
        rect = _include_caption(_table_crop_rect(page, raw), b.bbox, page)
        rect = _shrink_table_rect_off_figures(page, rect, blocks, b.page)
        if _table_swallows_page(b.page, rect, blocks):
            continue
        img = _crop(
            page, rect, out_dir / f"p{b.page:03d}_tbl_cap{i:04d}.png",
            target_px=TABLE_TARGET_PX,
            rotate=_table_text_rotation(page, raw),
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


_FIG_NUM_RE = re.compile(r"^\s*(?:Figure|Fig\.?)\s*([A-Z]?\d+)", re.IGNORECASE)


def _norm_caption(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _drop_duplicate_figures(blocks: list[Block]) -> list[Block]:
    """去掉同一 Figure 的重复抽取：子图标签块与整图、跨页同一图注碎片。

    `_extract_figures` 与矢量图兜底可能同时产出一张大图和一张子图/片段；
    跨页图注也可能让前一页只剩一条图注区域。保留图注更完整、面积更大的
    一张，避免阅读页同一张图出现两次。
    """
    figs = [b for b in blocks if b.kind == "figure" and (b.caption or "").strip()]
    if len(figs) < 2:
        return blocks
    drop: set[int] = set()
    for i, a in enumerate(figs):
        ma = _FIG_NUM_RE.match(a.caption)
        if not ma:
            continue
        for b in figs[i + 1:]:
            if id(a) in drop or id(b) in drop:
                continue
            mb = _FIG_NUM_RE.match(b.caption)
            if not mb or ma.group(1).upper() != mb.group(1).upper():
                continue
            ca, cb = len(a.caption.strip()), len(b.caption.strip())
            area_a = (a.bbox[2] - a.bbox[0]) * (a.bbox[3] - a.bbox[1])
            area_b = (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1])
            if a.page == b.page:
                cover = max(
                    _cover_ratio(pymupdf.Rect(a.bbox), pymupdf.Rect(b.bbox)),
                    _cover_ratio(pymupdf.Rect(b.bbox), pymupdf.Rect(a.bbox)),
                )
                if cover > 0.7:
                    if ca != cb:
                        drop.add(id(a) if ca < cb else id(b))
                    else:
                        drop.add(id(a) if area_a < area_b else id(b))
            elif abs(a.page - b.page) == 1 and _norm_caption(a.caption) == _norm_caption(b.caption):
                if area_a < area_b * 0.55:
                    drop.add(id(a))
                elif area_b < area_a * 0.55:
                    drop.add(id(b))
    out = [b for b in blocks if id(b) not in drop]
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
    return out


def _drop_float_text_fragments(blocks: list[Block], body_size: float) -> list[Block]:
    """丢掉落在图表裁图里的小字号短标注。

    位图/矢量图或表格已经画出这些坐标轴、图例、子图字母和表内标签；它们
    若还留在阅读流里，就会表现成「正文段落被识别进图片」——一条条 `A B`、
    `C`、`JQ1 (nM)` 单独成段。只删除小字号且短/少词的块，正文和长表格
    说明不会被误删。
    """
    floats = [b for b in blocks if b.kind in ("figure", "table")]
    if not floats:
        return blocks
    out: list[Block] = []
    for b in blocks:
        if b.kind == "para" and b.text.strip() and body_size > 0:
            text = b.text.strip()
            # 坐标轴方位/仪器标签常用与正文相近的字号（N / E / MIRI），
            # 不能只看「字号小于正文」；短全大写或少词标签直接按图内文字处理。
            short_label = len(text) <= 12 and (
                text.isupper() or len(_WORD_RE.findall(text)) <= 1
            )
            if 0 < b.size < body_size * 0.95 or short_label:
                br = pymupdf.Rect(b.bbox)
                covered = any(
                    f.page == b.page and _cover_ratio(br, pymupdf.Rect(f.bbox)) >= 0.7
                    for f in floats
                )
                if covered and (
                    len(text) <= 120
                    or len(_WORD_RE.findall(text)) <= 6
                ):
                    # 定义/引理/假设段的字号可能比正文略小，但句子完整、
                    # 词数多；它们是正文，不是图内坐标轴/图例标注。
                    prose = (
                        len(text) >= 80
                        and len(_WORD_RE.findall(text)) >= 12
                        and re.search(r"[.;:]\s", text)
                    )
                    if not prose:
                        continue
        out.append(b)
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
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
    detected: dict[int, list[tuple[pymupdf.Rect, str, int]]] | None = None,
) -> list[Block]:
    """表格抽取：先做结构化识别，再对漏网的表走裁图兜底。"""
    blocks = _extract_tables_structured(doc, blocks, out_dir, detected)
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


def _include_caption(
    crop: pymupdf.Rect,
    caption_bbox: tuple[float, float, float, float] | pymupdf.Rect,
    page: pymupdf.Page,
    pad: float = 2.0,
) -> pymupdf.Rect:
    """把图注/表注区域并进裁图，让标题出现在图片里。

    阅读页仍保留 caption 文本，标题会出现两次——这是用户明确接受的重复。
    """
    crop_rect = pymupdf.Rect(crop)
    cap_rect = pymupdf.Rect(caption_bbox)
    # 双栏页的表格/图与图注可能并排分属左右栏；把整块 caption 并进裁图
    # 会横向跨过 gutter，把另一栏正文一起裁进图表。只有 x 方向有足够
    # 重叠（上下紧邻）时才并入；并排 caption 交给阅读页单独显示。
    overlap_x = min(crop_rect.x1, cap_rect.x1) - max(crop_rect.x0, cap_rect.x0)
    if overlap_x < 0.25 * min(max(crop_rect.width, 1.0), max(cap_rect.width, 1.0)):
        return crop_rect
    out = crop_rect | cap_rect
    return pymupdf.Rect(
        max(page.rect.x0, out.x0 - pad),
        max(page.rect.y0, out.y0 - pad),
        min(page.rect.x1, out.x1 + pad),
        min(page.rect.y1, out.y1 + pad),
    )


def _crop(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    path: Path,
    dpi: int | None = None,
    target_px: int = CROP_TARGET_PX,
    dpi_max: int = CROP_DPI_MAX,
    rotate: int = 0,
) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if dpi is None:
            dpi = _dpi_for(rect, target_px, dpi_max)
        pix = page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
        # 横排大表在竖版页面里常被旋转 90° 放置；裁出来仍是竖条，
        # 阅读页必须把图像转正，否则整张表无法阅读。
        if rotate == 90:
            pix = pix.flip_rotate(pymupdf.Pixmap.ROTATE_90)
        elif rotate == 180:
            pix = pix.flip_rotate(pymupdf.Pixmap.ROTATE_180)
        elif rotate == 270:
            pix = pix.flip_rotate(pymupdf.Pixmap.ROTATE_270)
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
    gap_x: float = 36.0,
    gap_y: float = 20.0,
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


def _page_graphic_parts(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """页面上互相独立的位图 / 矢量簇，不做合并。

    `_figure_rects` 会把间距二十来 pt 的位图当成「一张图的子图」并起来；
    要按图注拆回去，必须先拿到合并前的原始块。
    """
    parts: list[pymupdf.Rect] = []
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"])
            if r.width * r.height > 500 and r.height > 10:
                parts.append(r)
    except Exception:
        pass
    try:
        for r in page.cluster_drawings():
            r = pymupdf.Rect(r)
            if (
                r.width * r.height > 4000
                and r.height > 40
                and not _rect_has_text(page, r)
            ):
                parts.append(r)
    except Exception:
        pass
    return parts


def _split_figure_rect_by_captions(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    captions: list[Block],
) -> list[pymupdf.Rect]:
    """把一个合并过头的图区按图注拆回「一张图一个区」。

    上下相邻的两张图只隔 20~30pt 时，`_merge_figure_regions` 会把它们并成
    一条整栏长图；随后一个图注吃掉全部内容，另一个图注变成孤儿。图注本身
    就是最好的分界：把每张图绑到离它最近的图注，同一图注下的子图沿同一
    方向并回来，中间夹着另一条图注时禁止跨越。
    """
    def same_column(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
        inter = min(a.x1, b.x1) - max(a.x0, b.x0)
        return inter > min(a.width, b.width) * 0.3

    inside = [
        c for c in captions
        if same_column(pymupdf.Rect(c.bbox), rect)
        and rect.y0 - 24 <= (c.bbox[1] + c.bbox[3]) / 2 <= rect.y1 + 90
    ]
    if len(inside) < 2:
        return [rect]

    parts = [
        part for part in _page_graphic_parts(page)
        if same_column(part, rect)
        and (_overlap_ratio(part, rect) > 0.2 or _rect_gap(part, rect) <= 4)
    ]
    if len(parts) < 2:
        return [rect]

    inside.sort(key=lambda c: c.bbox[1])
    caption_boxes = [pymupdf.Rect(c.bbox) for c in inside]

    def crosses_caption(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
        lo, hi = min(a.y1, b.y1), max(a.y0, b.y0)
        if hi <= lo:
            return False
        return any(c.y0 < hi - 1 and c.y1 > lo + 1 for c in caption_boxes)

    out: list[pymupdf.Rect] = []
    for cap in inside:
        cb = pymupdf.Rect(cap.bbox)
        if _rect_gap(cb, rect) > 120 and not cb.intersects(rect):
            continue
        near = [part for part in parts if same_column(part, cb)]
        if not near:
            continue
        anchor = min(near, key=lambda p: (_rect_gap(cb, p), abs(p.y0 - cb.y0)))
        if _rect_gap(cb, anchor) > 60:
            continue

        group = [anchor]
        side_up = (anchor.y0 + anchor.y1) / 2 < cb.y0
        changed = True
        while changed:
            changed = False
            merged = pymupdf.Rect(group[0])
            for item in group[1:]:
                merged |= item
            for part in parts:
                if any(part is g for g in group):
                    continue
                if not same_column(part, merged):
                    continue
                if _rect_gap(part, merged) > 30:
                    continue
                if side_up and part.y0 >= merged.y0:
                    continue
                if not side_up and part.y1 <= merged.y1:
                    continue
                if crosses_caption(part, merged):
                    continue
                group.append(part)
                changed = True
                break

        box = pymupdf.Rect(cb)
        for item in group:
            box |= item
        if side_up:
            box.y1 = max(box.y1, cb.y1)
        else:
            box.y0 = min(box.y0, cb.y0)
        box = pymupdf.Rect(rect.x0, box.y0, rect.x1, box.y1) & rect
        if box.height >= 40 and box.width >= 60:
            out.append(box)

    if len(out) < 2:
        return [rect]
    return out


def _figure_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """一页里的插图区域：位图 + 矢量图，合并去重后扩到图注/坐标轴标注。"""
    rects: list[pymupdf.Rect] = []
    try:
        infos = [
            pymupdf.Rect(info["bbox"]) for info in page.get_image_info()
        ]
    except Exception:
        infos = []
    # 子图矩阵的单块常常只有 80x60pt（面积不到 8000），逐个过滤会整张漏掉。
    # 先按行/列间距把相邻位图拼成一张，再按组合面积判断。
    for r in _merge_figure_regions(
        [r for r in infos if r.width * r.height > 500 and r.height > 10],
        gap_x=30.0,
        gap_y=30.0,
    ):
        # 4000pt² 会收进期刊 logo、二维码这类装饰图；8000 约等于
        # 90×90pt 的真实插图下限
        if r.width * r.height > 8000 and r.height > 40:
            rects.append(r)
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

    # 矢量散点图/箱线图由大量细碎图元组成（点、刻度线远小于 20pt），
    # 逐个过滤会把整张图漏掉；cluster_drawings 能把它们聚成真实图区。
    # 这里只收「区域内没有文字」的簇，带标注的矢量图交给图注锚定路径，
    # 且不再以「本页没有位图」为前提，混合页里的矢量图也能被看见。
    try:
        clusters = page.cluster_drawings()
    except Exception:
        clusters = []
    usable = sorted(
        [
            pymupdf.Rect(r)
            for r in clusters
            if pymupdf.Rect(r).width * pymupdf.Rect(r).height > 4000
            and pymupdf.Rect(r).height > 40
        ],
        key=lambda r: (r.y0, r.x0),
    )
    page_area = page.rect.width * page.rect.height
    groups: list[pymupdf.Rect] = []
    for r in usable:
        if _rect_has_text(page, r):
            continue
        # 同一张图的子图/分面之间只隔十几到几十 pt，先合回一张；
        # 间距更大的相邻图保持独立，避免把两张图缝成一张。
        if groups and _rect_gap(r, groups[-1]) <= 60:
            groups[-1] |= r
        else:
            groups.append(pymupdf.Rect(r))
    for g in groups:
        if g.width * g.height > page_area * 0.75:
            continue
        rects.append(g)

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
    page_rect = page.rect
    return pymupdf.Rect(
        max(page_rect.x0, out.x0 - 2),
        max(page_rect.y0, out.y0 - 2),
        min(page_rect.x1, out.x1 + 2),
        min(page_rect.y1, out.y1 + 2),
    )


def _trim_figure_rect_off_body(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    blocks: list[Block],
    page_no: int,
    body_size: float = 0.0,
) -> pymupdf.Rect:
    """图区把页面上方/下方的成段正文框进来时，收缩到正文之外。

    矢量图的背景框/外框常向上延到页顶，`_figure_crop_rect` 会把整圈边框
    并进来，连页顶的章节正文一起裁进图片（Figure 4 的图区曾盖住 3.2 节
    正文）。只按「正文字号 + 成段长度 + 整栏宽」收缩，图内小字标注不受影响。
    """
    out = pymupdf.Rect(rect)
    for b in blocks:
        if b.page != page_no or not _is_body_block(b):
            continue
        if body_size > 0 and 0 < b.size < body_size * 0.9:
            continue
        if len(b.text.strip()) < 120:
            continue
        # 双栏页的单栏正文只有半页宽，按 0.5 页宽判会永远放行，
        # 图区于是把整段正文一起裁进图片（Figure 1 曾上方压了 859 字正文）。
        if (b.bbox[2] - b.bbox[0]) < page.rect.width * 0.28:
            continue
        br = pymupdf.Rect(b.bbox)
        if _cover_ratio(br, out) < 0.3:
            continue
        if (br.y0 + br.y1) / 2 < (out.y0 + out.y1) / 2:
            out.y0 = max(out.y0, br.y1 + 2)
        else:
            out.y1 = min(out.y1, br.y0 - 2)
    if out.height < 40 or out.width < 60:
        return rect
    return out


def _extract_figures(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
    body_size: float = 0.0,
    detected: dict[int, list[tuple[pymupdf.Rect, str, int]]] | None = None,

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
        # find_tables 的原始框比跨页续表块更可信，但它自己也可能把表 + 图
        # 整块框成一页大表。两者取交集做遮挡判断最保守：既不会因为续表
        # 块横跨两栏而丢图（图 3/图 4 曾消失），也不会因为 detected 吞掉
        # 图区而丢图（Figure 2 曾整张不见）。
        trusted = [
            pymupdf.Rect(r) for r, _html, _cols in (detected or {}).get(page_no, [])
        ]
        if trusted:
            combined: list[pymupdf.Rect] = []
            for r in table_rects:
                best = max(trusted, key=lambda t: _overlap_ratio(r, t))
                if _overlap_ratio(r, best) <= 0.1:
                    # 与 find_tables 对不上，多半是跨页续表或整页误判
                    continue
                inter = r & best
                if not inter.is_empty:
                    combined.append(inter)
            table_rects = _merge_rects(combined) if combined else trusted
        rects = _figure_rects(page)
        # 这里不并入区域层的图区：区域层是按「矢量簇」给的，一张流程图常被
        # cluster_drawings 拆成左右两列（间距 40pt），直接并进来会裁出多张
        # 切片。带文字标注的矢量图仍交给 `_extract_vector_text_figures` 按
        # 图注锚定成一张；区域层的作用是先把图内文字挡在正文流之外。
        if not rects:
            continue

        captions = [
            b for b in blocks
            if b.page == page_no and b.kind == "caption" and not _is_table_caption(b.text)
        ]
        # 上下相邻的两张图只隔二十来 pt 时，位图合并会把它们连成一整栏；
        # 图注是天然分界，先按图注拆回独立图区，再走配对与合并。
        if len(captions) >= 2:
            split: list[pymupdf.Rect] = []
            for r in rects:
                split.extend(_split_figure_rect_by_captions(page, r, captions))
            if len(split) > len(rects):
                rects = split
        # 图区比图注多：多半是一张跨栏图被拆成左右子图，间距会超过
        # 常规的 30pt；按图注数量再合并一次，避免一半的图没有标题。
        # 整页只有一个图注、却切出三块以上，通常是子图矩阵（2x4 瀑布图等），
        # 间距会到 50~70pt，需要更大的合并阈值才能拼回一张。
        if 0 < len(captions) < len(rects):
            big_grid = len(captions) == 1 and len(rects) >= 3
            rects = _merge_figure_regions(
                rects,
                gap_x=60.0 if big_grid else 42.0,
                gap_y=80.0 if big_grid else 24.0,
            )
        used: set[str] = set()
        for k, r in enumerate(rects):
            r = _trim_figure_rect_off_body(page, r, blocks, page_no, body_size)
            # 表格已经有独立切片，图片识别不能再把同一块区域当插图重复放一次
            if any(_overlap_ratio(r, t) > 0.25 for t in table_rects):
                continue
            # 就近配对：常见图注在图片下方，少数期刊在上方。
            # 位图 bbox 常把紧贴的图注一起框进来（图注与图区重叠十几 pt），
            # 容差放到 -30 否则这种图永远配不上标题。
            below = [
                c for c in captions
                if c.id not in used and -80 <= c.bbox[1] - r.y1 <= 200
            ]
            above = [
                c for c in captions
                if c.id not in used and -30 <= r.y0 - c.bbox[3] <= 150
            ]
            cap = None
            side = "below"
            if below:
                cap = min(below, key=lambda c: abs(c.bbox[1] - r.y1))
            elif above:
                cap = min(above, key=lambda c: abs(r.y0 - c.bbox[3]))
                side = "above"
            if cap:
                # 标题一起裁进图片；界面上的 caption 仍保留，允许重复显示
                r = _include_caption(r, cap.bbox, page)

            img = _crop(page, r, out_dir / f"p{page_no:03d}_fig{k}.png")
            if not img:
                continue
            fig = Block(
                id=f"fig_p{page_no}_{k}", order=0, page=page_no, kind="figure",
                text="", bbox=(r.x0, r.y0, r.x1, r.y1), image_path=img,
                translatable=False,
            )
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
        # 没有配到图注的图不能留空：阅读页会出现一个只有图片、标题全空的块。
        # 若它紧贴同页表格，按「表 X 附图」给出上下文；否则明确标成页码图。
        # 但已有配好图注的图覆盖了同一区域时不能再插一张，否则同一张图
        # 会在阅读页出现两次（Fig. 1 曾被拆出一个「第 3 页图」重复块）。
        existing = [
            pymupdf.Rect(b.bbox) for b in result
            if b.page == page_no and b.kind == "figure"
        ]
        figs = [
            f for f in figs
            if not any(_cover_ratio(pymupdf.Rect(f.bbox), r) > 0.35 for r in existing)
        ]
        if not figs:
            continue
        page_tables = [
            b for b in blocks
            if b.page == page_no and b.kind == "table" and (b.caption or "").strip()
        ]
        for fig in figs:
            if (fig.caption or "").strip():
                continue
            fr = pymupdf.Rect(fig.bbox)
            near = None
            for t in page_tables:
                tr = pymupdf.Rect(t.bbox)
                if (
                    _rect_gap(fr, tr) <= 20
                    and min(fr.x1, tr.x1) - max(fr.x0, tr.x0) > 0
                ):
                    near = t
                    break
            fig.caption = f"{near.caption}（附图）" if near else f"第 {page_no} 页图"
        idx = max(
            (i for i, b in enumerate(result) if b.page == page_no),
            default=len(result) - 1,
        )
        result[idx + 1:idx + 1] = figs

    for i, b in enumerate(result):
        b.order = i
        b.id = f"b{i:04d}"
    return result



def _is_text_paragraph(b: Block, page: pymupdf.Page) -> bool:
    """按「长度 + 行高 + 栏宽」判断是否成段正文，兼容中文短句。

    英文 55 字符大约等于中文 30 余字；只按字符数卡 55 会让中文短段
    （一整句话三四十字）漏过，被兜底路径当成图内标注收进裁图。
    """
    text = (b.text or "").strip()
    if not text:
        return False
    width = b.bbox[2] - b.bbox[0]
    height = b.bbox[3] - b.bbox[1]
    # 必须两行以上：表格单元格里的长题干也是「够长 + 够宽」，但只有一行，
    # 把它当正文会让合法的大表被判成「正文被当表」而整页丢掉。
    if len(text) > 55 and width > page.rect.width * 0.3 and height > 22:
        return True
    return len(text) > 30 and height > 22 and width > page.rect.width * 0.22


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


def _crop_inline_math(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
) -> list[Block]:
    """复杂行内公式（分式线、伸缩符号等）裁成小图内联显示。

    规则能可靠重建的走 LaTeX；规则覆盖不到的保留原裁图，保证字母和排版
    不会因为猜错而变形。
    """
    # 同篇内相同公式片段（如重复出现的 √、希腊字母集合）共享同一张裁图，
    # 避免公式密集论文产生成百上千张重复 PNG。
    cache: dict[tuple[str, int], str] = {}
    for b in blocks:
        # 图注/表注/参考文献里也可能有行内公式；前端 RichText 同样按
        # inline_math 渲染，漏裁会让这些位置留下方块。
        if b.kind not in ("para", "abstract", "list", "caption", "reference"):
            continue
        for index, item in enumerate(b.inline_math):
            if not item.get("fallback") or item.get("image_path"):
                continue
            bbox = item.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            rect = pymupdf.Rect(bbox)
            if rect.is_empty or rect.width < 1 or rect.height < 1:
                continue
            key = ((item.get("source") or "").strip(), round(rect.height))
            cached = cache.get(key)
            if cached and Path(cached).exists():
                item["image_path"] = cached
                item["latex"] = ""
                continue
            rect = pymupdf.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1)
            page = doc[b.page - 1]
            path = _crop(
                page,
                rect,
                out_dir / f"im_{b.order:04d}_{index}.png",
                target_px=600,
                dpi_max=300,
            )
            if path:
                item["image_path"] = path
                item["latex"] = ""
                cache[key] = path
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


def _is_small_float_text(b: Block, body_size: float) -> bool:
    """字号明显小于正文的是图内标注（Nature 整页图的说明常用 5pt）。

    只靠长度会把「Scientist describes a research goal…」这类长标注判成
    正文，进而阻止整页图裁图；按字号再收一道。
    """
    return bool(b.size) and body_size > 0 and b.size < body_size * 0.8


def _extract_vector_text_figures(
    doc: pymupdf.Document,
    blocks: list[Block],
    out_dir: Path,
    body_size: float = 0.0,
    page_regions: dict[int, PageRegions] | None = None,
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
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        if len(drawings) < 3:
            continue

        page_region = (page_regions or {}).get(page_no)
        region_figures = (
            [pymupdf.Rect(r.bbox) for r in page_region.figures]
            if page_region is not None else []
        )
        texts = [
            b for _, b in items
            if b.kind not in ("figure", "table", "reference", "footnote")
            and b.text.strip()
        ]
        # 图内标注已被区域层剔出正文流时，texts 会很少；只要有区域图区
        # 就不能因此放弃这一页。
        if len(texts) < 4 and not region_figures:
            continue
        disclaim = lambda b: _is_small_float_text(b, body_size)  # noqa: E731
        fig_rects = [
            pymupdf.Rect(b.bbox) for _, b in items if b.kind == "figure"
        ]
        covered = {
            b.caption.strip() for _, b in items
            if b.kind == "figure" and b.caption.strip()
        }
        # 表格自己也是矢量簇 / 线条，兜底路径并入图形时要把它们排除，
        # 否则图区会压住表格、被「图不能压表格」校验整张否掉。
        table_guards = [
            pymupdf.Rect(b.bbox) for _, b in items if b.kind == "table"
        ]
        caps = sorted(
            [
                b for b in texts
                if b.kind == "caption"
                and not _is_table_caption(b.text)
                and b.text.strip() not in covered
            ],
            key=lambda b: b.bbox[1],
        )
        if not caps:
            # 整页图的图注常被排到下一页，页面上只留「See next page」。
            nxt = [
                b for b in blocks
                if b.page == page_no + 1
                and b.kind == "caption"
                and b.text.strip()
                and not _is_table_caption(b.text)
            ]
            if nxt:
                caps = nxt[:1]
        if not caps:
            continue

        # 图注锚定的大块图形区域（矢量簇 + 位图）。图内文字可能和正文
        # 同字号、同宽度（Beyond Accuracy 的 Figure 1 是 9pt 图文卡片），
        # 单靠字号无法区分，只能承认「落在这块图区里的文字属于图」。
        try:
            clusters = [pymupdf.Rect(r) for r in page.cluster_drawings()]
        except Exception:
            clusters = []
        try:
            images = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
        except Exception:
            images = []
        page_area = page.rect.width * page.rect.height
        if region_figures:
            # 区域层已按视觉单元把图区分好（含图内标注）。这里直接拿它
            # 当候选，不再依赖 cluster_drawings 的二次聚类与图内文字生长；
            # 锚定这一步只负责把它和图注配对。
            regions = [
                r for r in region_figures if r.width * r.height < page_area * 0.75
            ]
        else:
            regions = [
                r for r in _merge_figure_regions(
                    [
                        r for r in [*clusters, *images]
                        if r.width * r.height > 4000 and r.height > 40
                    ],
                    gap_x=40.0,
                    gap_y=40.0,
                )
                if r.width * r.height < page_area * 0.75
            ]
        anchors: list[pymupdf.Rect] = []
        for r in regions:
            # 表格自己也是矢量簇/线段，绝不能再被图注锚定成 figure。
            # 旋转表整页只有表框时，这里会把同页的「图注」错误配给表格区域。
            if any(_overlap_ratio(r, t) > 0.25 for t in table_guards):
                continue
            for cap in caps:
                cb = pymupdf.Rect(cap.bbox)
                overlap_x = min(r.x1, cb.x1) - max(r.x0, cb.x0)
                if overlap_x <= min(r.width, cb.width) * 0.3:
                    continue
                # 图注可能排在整页图的下方很远（Figure S2 这类补充材料，
                # 图本体到图注常超过 90pt）。区域层已经保证图区不含正文，
                # 放宽距离只影响「配哪条图注」，不会再吞正文。
                if -30 <= cb.y0 - r.y1 <= 200 or -30 <= r.y0 - cb.y1 <= 200:
                    anchors.append(r | cb)
                    break

        def anchored(b: Block) -> bool:
            if not anchors:
                return False
            br = pymupdf.Rect(b.bbox)
            return any(_cover_ratio(br, ar) > 0.6 for ar in anchors)

        long = sum(
            1 for b in texts
            if _is_body_block(b) and not disclaim(b) and not anchored(b)
            and len(b.text.strip()) > 220
        )
        if long > 6:
            continue

        # 「正文」的判据必须和 collect_side 一致：8.5pt 对 10pt 正文的图内
        # 长标注不算正文，否则它会同时进入 body_rects，导致图区一覆盖它
        # 就被「图不能压正文」否掉（RingFormer 的 Figure 3 就是这样漏的）。
        def is_body(b: Block) -> bool:
            if not _is_body_block(b) or disclaim(b) or anchored(b):
                return False
            return not (body_size > 0 and 0 < b.size < body_size * 0.9)

        bodies = [b for b in texts if is_body(b)]
        body_rects = [pymupdf.Rect(b.bbox) for b in bodies] + [
            pymupdf.Rect(b.bbox) for _, b in items if b.kind == "table"
        ]

        def collect_side(cap: Block, step: int) -> list[Block]:
            """从图注向图的方向区域生长，收集图内标注，正文块作为边界不加入。

            多列版面里同一水平带有左右两个框，按 y 顺序逐块扫描会把后一个
            框的底部当成「反向重叠」跳过；这里改成按与当前图区的距离生长。
            """
            band_lo = page.rect.y0
            band_hi = page.rect.y1
            for other in caps:
                if other is cap:
                    continue
                if other.bbox[1] < cap.bbox[1]:
                    band_lo = max(band_lo, other.bbox[3])
                else:
                    band_hi = min(band_hi, other.bbox[1])
            if step < 0:
                band_hi = min(band_hi, cap.bbox[1])
            else:
                band_lo = max(band_lo, cap.bbox[3])

            rect = pymupdf.Rect()
            picked: list[Block] = []
            seen: set[str] = set()
            changed = True
            while changed:
                changed = False
                ref = rect if not rect.is_empty else pymupdf.Rect(cap.bbox)
                for b in texts:
                    if b is cap or b.id in seen:
                        continue
                    if b.kind in ("caption", "table", "figure", "reference"):
                        continue
                    cy = (b.bbox[1] + b.bbox[3]) / 2
                    if not (band_lo - 2 <= cy <= band_hi + 2):
                        continue
                    if step < 0 and b.bbox[1] >= cap.bbox[1] - 1:
                        continue
                    if step > 0 and b.bbox[3] <= cap.bbox[3] + 1:
                        continue
                    # 正文标题（字号与正文相当）是页面结构，不是图内标注；
                    # 图内标题字号通常明显小于正文，仍会被收进图区。
                    if b.kind in ("heading", "title"):
                        if (body_size <= 0 or b.size >= body_size * 0.95) and not anchored(b):
                            continue
                    # 完整长句（定义/引理/假设）是正文：图注 bbox 可能压到
                    # 本行，`anchored` 因此为真，但不能把它收进图内标注。
                    prose = (
                        len(b.text.strip()) >= 80
                        and len(b.text.split()) >= 12
                        and bool(re.search(r"[.;:]\s", b.text))
                    )
                    if prose and not disclaim(b):
                        continue
                    # 正文风格（正文字号 + 够长 + 够宽）是图的边界；
                    # 图内长标注（8.5pt 对 10pt 正文）字号略小，不能按
                    # 「≥0.95 倍正文」当正文，否则整张图会被下文挡住。
                    small = body_size > 0 and 0 < b.size < body_size * 0.9
                    if not disclaim(b) and not small and not anchored(b) and (
                        _is_body_block(b)
                        or (
                            body_size > 0
                            and b.size >= body_size * 0.95
                            and _is_text_paragraph(b, page)
                        )
                    ):
                        continue
                    br = pymupdf.Rect(b.bbox)
                    if _rect_gap(br, ref) > 160:
                        continue
                    picked.append(b)
                    seen.add(b.id)
                    rect |= br
                    changed = True
            return picked

        for idx, cap in enumerate(caps):
            up = collect_side(cap, -1)
            down = collect_side(cap, 1)
            side = "below" if len(down) > len(up) else "above"
            fig_texts = down if len(down) > len(up) else up
            if len(fig_texts) >= 2:
                # 不能只看平均长度：图内说明性标注本身就可能很长（RingFormer
                # Figure 3 的 (a) 标注 355 字符），会把整张图误判成正文。
                # 只有「正文字号 + 长句」出现多块时才认为定位错了。
                big = sum(
                    1 for b in fig_texts
                    if body_size > 0 and b.size >= body_size * 0.95
                    and _is_text_paragraph(b, page) and not anchored(b)
                )
                if big > 1 or sum(len(b.text.strip()) for b in fig_texts) / len(fig_texts) > 150:
                    fig_texts = []
            else:
                fig_texts = []
            if fig_texts:
                rect = pymupdf.Rect()
                for b in fig_texts:
                    rect |= pymupdf.Rect(b.bbox)
            else:
                # 纯图形没有可收集的文字标注（热图、示意图）：用图注相邻的
                # 图形簇/位图直接成图，否则整张图会因为没有文字而漏掉。
                cb = pymupdf.Rect(cap.bbox)
                # 只取「与图注真正相邻」的图形簇，不要先把整页所有簇合并：
                # 表格簇和热图簇只隔 20 多 pt，先合并会把表格一起框进图区，
                # 随后被「图不能压表格」整张否掉（Figure 4 就是这样丢的）。
                near = [
                    r for r in [*clusters, *images, *region_figures]
                    if r.width * r.height > 4000 and r.height > 40
                    and min(r.x1, cb.x1) - max(r.x0, cb.x0) > min(r.width, cb.width) * 0.3
                    and (
                        -30 <= cb.y0 - r.y1 <= 200
                        or -30 <= r.y0 - cb.y1 <= 200
                    )
                ]
                if not near:
                    continue
                rect = pymupdf.Rect()
                for r in near:
                    rect |= r
                rect |= cb
            # 并入与图区相交/紧邻的线条（坐标轴、图例框、流程图边框）
            for d in drawings:
                dr = pymupdf.Rect(d["rect"])
                if dr.is_empty or dr.height <= 1:
                    continue
                # 页眉/页脚横线、整页背景块横跨两栏，并进来会把图区拉到
                # 覆盖正文，随后被「图不能压正文」校验整张否掉。
                if dr.width > page.rect.width * 0.6 and dr.height < 8:
                    continue
                if dr.width * dr.height > page.rect.width * page.rect.height * 0.2:
                    continue
                # 表格自己的边框也属于 drawings，并进来会让图区压住表格，
                # 随后整张图被否掉（Figure 4 曾因此消失）。
                if any(_overlap_ratio(dr, t) > 0.3 for t in table_guards):
                    continue
                if _overlap_ratio(dr, rect) > 0 or _rect_gap(dr, rect) <= 12:
                    rect |= dr
            # 流程图/子图常被 cluster_drawings 拆成相邻几块，按近邻再并一次
            try:
                for cr in page.cluster_drawings():
                    cr = pymupdf.Rect(cr)
                    if cr.width * cr.height < 1200:
                        continue
                    if any(_overlap_ratio(cr, t) > 0.3 for t in table_guards):
                        continue
                    if _overlap_ratio(cr, rect) > 0 or _rect_gap(cr, rect) <= 30:
                        rect |= cr
            except Exception:
                pass
            rect = pymupdf.Rect(
                max(page.rect.x0, rect.x0 - 4),
                max(page.rect.y0, rect.y0 - 4),
                min(page.rect.x1, rect.x1 + 4),
                min(page.rect.y1, rect.y1 + 4),
            )
            # 兜底路径按图注向外生长时，容易把上方/下方的整段正文并进来
            # （Figure 1 曾把 859 字正文裁进图片）；用与主路径相同的
            # 正文边界再收缩一次。
            rect = _trim_figure_rect_off_body(page, rect, blocks, page_no, body_size)
            # 通栏图的图注通常横跨整页，但图内标注的文字收集可能只覆盖左栏；
            # 按图注宽度把图区放宽回通栏，否则右半张图会被裁掉。
            if (cap.bbox[2] - cap.bbox[0]) > page.rect.width * 0.5:
                rect.x0 = min(rect.x0, cap.bbox[0] - 2)
                rect.x1 = max(rect.x1, cap.bbox[2] + 2)
            # 图注明明在单栏里，却生长出一条跨两栏的横条：兜底路径把邻栏
            # 的表格行、正文短段当成了图内标注。按图注所在栏收窄，收窄后
            # 明显仍跨栏的就不生成，避免裁出「一整列」的错误图片。
            mid = page.rect.width / 2
            cap_cx = (cap.bbox[0] + cap.bbox[2]) / 2
            # 图本身是通栏（有横跨两栏的矢量簇 / 位图）时不能按图注所在栏收窄，
            # 否则 Figure 1 这类「图注排在左栏、图横跨整页」的图会被裁掉右半。
            wide_graphic = False
            try:
                for cr in page.cluster_drawings():
                    cr = pymupdf.Rect(cr)
                    if (
                        cr.width > page.rect.width * 0.5
                        and _overlap_ratio(cr, rect) > 0.3
                    ):
                        wide_graphic = True
                        break
            except Exception:
                pass
            if (
                not wide_graphic
                and rect.width > page.rect.width * 0.62
                and abs(cap_cx - mid) > page.rect.width * 0.08
            ):
                if cap_cx < mid and rect.x1 > mid + 6:
                    rect.x1 = mid - 2
                elif cap_cx >= mid and rect.x0 < mid - 6:
                    rect.x0 = mid + 2
            # 图形区域不能压到正文块，否则说明图注定位错了
            if rect.width < 60 or rect.height < 40:
                continue
            if any(_cover_ratio(br, rect) > 0.5 for br in body_rects):
                continue
            # 已有 figure 覆盖了同一区域就不重复裁一张
            if any(_cover_ratio(fr, rect) > 0.5 for fr in fig_rects):
                continue

            # 标题一起裁进图片（界面 caption 仍保留，允许重复）
            rect = _include_caption(rect, cap.bbox, page)

            img = _crop(page, rect, out_dir / f"p{page_no:03d}_vec{idx}.png")
            if not img:
                continue
            # 兜底裁图与已有的「第 N 页图」占位块大面积重合时，删掉占位块，
            # 否则同一张图在阅读页会出现两次（Fig. 1 曾多出一条重复横条）。
            for old in blocks:
                if old.kind != "figure" or old.id in drop:
                    continue
                old_cap = (old.caption or "").strip()
                if not (old_cap.startswith("第 ") or old_cap.endswith("（附图）")):
                    continue
                if _cover_ratio(pymupdf.Rect(old.bbox), rect) > 0.35:
                    drop.add(old.id)
            fig = Block(
                id=f"vec_p{page_no}_{idx}", order=0, page=page_no, kind="figure",
                text="", bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                image_path=img, caption=cap.text, translatable=False,
            )
            if side == "above":
                insert_before.setdefault(cap.id, []).append(fig)
            else:
                insert_after.setdefault(cap.id, []).append(fig)
            drop.update(b.id for b in fig_texts)

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
    body_size: float = 0.0,
    page_regions: dict[int, PageRegions] | None = None,
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

    # 整页插图在建块后可能一个块都不剩（图内标注被区域层剔除、图注在
    # 下一页），这样的页在 by_page 里根本不存在。必须把「区域层认出的
    # 图页」也纳入遍历，否则整张图连同它的兜底路径一起消失。
    page_numbers = sorted(set(by_page) | {
        p for p, region in (page_regions or {}).items() if region.figures
    })

    replacements: dict[int, tuple[int, Block]] = {}
    inserts: dict[int, list[Block]] = {}
    for page_no in page_numbers:
        if page_no < 1 or page_no > len(doc):
            continue
        items = by_page.get(page_no, [])
        page = doc[page_no - 1]
        page_region = (page_regions or {}).get(page_no)
        # 已经有位图/表格块的页面交给原来的图表抽取逻辑，避免重复
        if any(b.kind in ("figure", "table") for _, b in items):
            continue
        if page.get_images(full=True) or len(page.get_drawings()) < 200:
            continue

        texts = [
            (i, b) for i, b in items
            if b.kind not in ("figure", "table", "reference") and b.text.strip()
        ]
        # 区域层兜底：整页插图（Figure S2 这类）在建块后可能只剩一个小节
        # 标题和图注——图内标注已经被区域层剔出正文流，既凑不够「至少 8 个
        # 短文本块」，也没有图内文字可供锚定生长，兜底路径会整张漏掉。
        #
        # 关键：裁的是**图区 bbox**，不是整页渲染。整页渲染会把同页两张图
        # 合并成一张、把页眉页码一起裁进来，正是用户看到的「整页切片」。
        # 这条分支只在其他抽图路径都没产出 figure 时才生效，避免重复裁图。
        page_area = page.rect.width * page.rect.height
        region_fig = None
        if (
            page_region is not None
            and page_region.figures
            and not any(b.kind == "figure" for _, b in items)
        ):
            para_chars = sum(len(b.text.strip()) for _, b in items if b.kind == "para")
            if para_chars <= 80:
                candidate = max(page_region.figures, key=lambda r: r.area)
                if candidate.area >= page_area * 0.35:
                    region_fig = candidate

        if region_fig is not None:
            rect = pymupdf.Rect(region_fig.bbox)
            img = _crop(page, rect, out_dir / f"p{page_no:03d}_region.png")
            if img:
                caps = [
                    b.text.strip() for _, b in items
                    if b.kind == "caption" and b.text.strip()
                ]
                if not caps or all("next page" in c.lower() for c in caps):
                    nxt = [
                        b.text.strip() for b in blocks
                        if b.page == page_no + 1
                        and b.kind == "caption"
                        and b.text.strip()
                        and not _is_table_caption(b.text)
                    ]
                    if nxt:
                        caps = nxt[:1]
                fig = Block(
                    id="", order=0, page=page_no, kind="figure", text="",
                    bbox=tuple(region_fig.bbox), image_path=img,
                    caption=caps[0] if caps else f"第 {page_no} 页图",
                    translatable=False,
                )
                anchor = (
                    items[0][0] + 1 if items
                    else min(
                        (i for i, b in enumerate(blocks) if b.page > page_no),
                        default=len(blocks),
                    )
                )
                inserts.setdefault(anchor, []).append(fig)
            continue

        if len(texts) < 8:
            continue
        short = sum(1 for _, b in texts if len(b.text.strip()) <= 70)
        long = sum(
            1 for _, b in texts
            if len(b.text.strip()) > 220 and not _is_small_float_text(b, body_size)
        )
        if short < len(texts) * 0.7 or long > 2:
            continue
        # 页面里只要还有成段的正文，就不能整页图化：整页替换会把这些正文
        # 一起吞进图片（引言、附录说明、图下的分析段都遇到过）。交给常规的
        # 图表抽取，只把矢量图形裁成 figure，正文继续按段落渲染。
        # 字号明显小于正文的块是图内标注，不算「成段正文」。
        #
        # 旧判据是「存在单个 >80 字的正文块」，它依赖段落至少有一个长块。
        # 而段落被上游规则拆碎时这个条件恰好不成立，于是「段落拆碎 → 长块
        # 消失 → 守卫失效 → 整页图化」形成闭环，正文被整页裁进图片。
        # 改为先看「非图区正文的字符总量」：碎成多个短块也照样达标。
        body_chars = sum(
            len(b.text.strip()) for _, b in texts
            if b.kind in ("para", "heading")
            and not _is_small_float_text(b, body_size)
            and not (
                page_region is not None
                and page_region.figure_at(b.bbox) is not None
            )
        )
        if body_chars > 240 or any(
            b.kind in ("para", "heading")
            and len(b.text.strip()) > 80
            and not _is_small_float_text(b, body_size)
            for _, b in texts
        ):
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        img = out_dir / f"page_{page_no:03d}.png"
        page.get_pixmap(dpi=_dpi_for(page.rect, CROP_TARGET_PX, 300)).save(str(img))
        first = min((i for i, _ in texts), default=min((i for i, _ in items), default=0))
        # 用页面上真实的图注，而不是笼统的「第 N 页图表」；没有图注才兜底
        caps = [b.text.strip() for _, b in items if b.kind == "caption" and b.text.strip()]
        # 整页图常把「See next page for caption」印在图底，真正的图注在下一页；
        # 这种占位句不能当图注，往下一页找真实编号图注。
        if not caps or all("next page" in c.lower() for c in caps):
            nxt = [
                b.text.strip() for b in blocks
                if b.page == page_no + 1
                and b.kind == "caption"
                and b.text.strip()
                and not _is_table_caption(b.text)
            ]
            if nxt:
                caps = nxt[:1]
        fig = Block(
            id="",
            order=0,
            page=page_no,
            kind="figure",
            text="",
            bbox=(0.0, 0.0, page.rect.width, page.rect.height),
            image_path=str(img),
            caption=caps[0] if caps else f"第 {page_no} 页图表",
            translatable=False,
        )
        if items:
            replacements[page_no] = (first, fig)
        else:
            # 整页图在建块后没有留下任何块，无法「替换首块」，只能按页码
            # 找到它在阅读顺序里的插入点。
            anchor = min(
                (i for i, b in enumerate(blocks) if b.page > page_no),
                default=len(blocks),
            )
            inserts.setdefault(anchor, []).append(fig)

    if not replacements and not inserts:
        return blocks

    out: list[Block] = []
    for i, b in enumerate(blocks):
        out.extend(inserts.get(i, []))
        repl = replacements.get(b.page)
        if repl is not None:
            if i == repl[0]:
                out.append(repl[1])
            continue
        out.append(b)
    out.extend(inserts.get(len(blocks), []))
    for i, b in enumerate(out):
        b.order = i
        b.id = f"b{i:04d}"
    return out


def _extract_ruled_algorithms(doc, blocks: list[Block], out_dir: Path) -> list[Block]:
    """Recover captioned algorithm floats bounded by matching horizontal rules."""
    additions: list[Block] = []
    for pno, page in enumerate(doc, 1):
        ruled_tables = [
            pymupdf.Rect(b.bbox) for b in blocks
            if b.page == pno and b.kind == "table"
        ]
        rules = []
        for drawing in page.get_drawings():
            rect = pymupdf.Rect(drawing["rect"])
            if rect.width >= 80 and rect.height <= 2:
                rules.append(rect)
        for raw in page.get_text("dict")["blocks"]:
            for line in raw.get("lines", []):
                title = "".join(span["text"] for span in line["spans"]).strip()
                # 题注层已把 Algorithm 纳入表题体系；这里复用同一判据，
                # 顺序编号与附录编号（Algorithm A.1）都能兜底。
                if not re.match(r"^(?:Algorithm|ALGORITHM)\s", title):
                    continue
                if not _is_table_caption(title):
                    continue
                heading = pymupdf.Rect(line["bbox"])
                if any(
                    t.x0 - 2 <= (heading.x0 + heading.x1) / 2 <= t.x1 + 2
                    and t.y0 - 2 <= (heading.y0 + heading.y1) / 2 <= t.y1 + 2
                    for t in ruled_tables
                ):
                    # 结构化表格路径已经产出该算法框，避免重复裁一张
                    continue
                tops = [r for r in rules if heading.y0 - 15 <= r.y0 <= heading.y1
                        and r.x0 <= heading.x0 + 8 and r.x1 >= heading.x1 - 8]
                if not tops:
                    continue
                top = min(tops, key=lambda r: abs(r.y0 - heading.y0))
                bottoms = [r for r in rules if r.y0 > heading.y1 + 25
                           and abs(r.x0 - top.x0) < 5 and abs(r.x1 - top.x1) < 5]
                if not bottoms:
                    continue
                bottom = min(bottoms, key=lambda r: r.y0)
                rect = pymupdf.Rect(top.x0 - 2, min(top.y0, heading.y0) - 2,
                                    top.x1 + 2, bottom.y0 + 2) & page.rect
                img = _crop(page, rect, out_dir / f"p{pno:03d}_algorithm{len(additions)}.png", target_px=TABLE_TARGET_PX)
                if img:
                    additions.append(Block(id='', order=0, page=pno, kind='table', text='',
                                           bbox=tuple(rect), image_path=img, caption=title,
                                           translatable=False))
    for algorithm in additions:
        rect = pymupdf.Rect(algorithm.bbox)
        inside = [i for i, b in enumerate(blocks) if b.page == algorithm.page
                  and rect.contains(pymupdf.Point((b.bbox[0] + b.bbox[2]) / 2,
                                                   (b.bbox[1] + b.bbox[3]) / 2))]
        if inside:
            pos = inside[0]
            blocks = [b for i, b in enumerate(blocks) if i not in set(inside)]
            blocks.insert(pos, algorithm)
        else:
            pos = next ( (i for i, b in enumerate(blocks) if b.page > algorithm.page
                          or (b.page == algorithm.page and b.bbox[1] > rect.y1)), len(blocks))
            blocks.insert(pos, algorithm)
    for i, b in enumerate(blocks):
        b.order, b.id = i, f"b{i:04d}"
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
        rows = _merge_visual_rows(lines, gutters)      # 行内公式碎片并回正文行
        page_width = doc[0].rect.width
        page_height = doc[0].rect.height
        body_size = _guess_body_size(rows)
        # 首页前置区（标题/作者卡片/摘要/关键词）先单独建模：它的版式
        # 与正文「单栏/双栏」不同，三列作者卡片不能交给正文 gutter 排序。
        front = _detect_front_matter(rows, body_size, page_height, page_width)
        ordered = _reading_order(
            rows, 0.0 if has_gutter else None, front, page_width
        )

        # 表格区域要先于段落重组拿到：表格行一旦被合并成大段，
        # 兜底切表就拿不到「多行短块」，整张表会漏识别。
        detected = _find_page_tables(doc)
        table_rects: dict[int, list[pymupdf.Rect]] = {}
        for page_no, items in detected.items():
            page = doc[page_no - 1]
            page_area = page.rect.width * page.rect.height
            rects = [
                rect for rect, _html, _cols in items
                if rect.width * rect.height <= page_area * 0.6
                # find_tables 在缺少 pymupdf.layout 的发布包里会退回旧算法，
                # 输出覆盖整页的伪表框；建块前必须按框内文本形态否决，否则
                # 整页正文会被逐行成块。
                and not _table_rect_covers_body_text(
                    lines, page_no, rect, page.rect.height, page_area
                )
            ]
            if rects:
                table_rects[page_no] = rects
        page_regions = build_page_regions(
            doc, lines, body_size, table_rects, _is_caption_text,
        )
        blocks = _build_blocks(
            ordered, body_size, table_rects, page_regions, front
        )
        _classify(blocks, body_size, page_height, front)
        blocks = _promote_short_headings(blocks, body_size)
        side_bands = _detect_side_bands(ordered, page_width)
        blocks = _split_cross_column_blocks(
            blocks, ordered, gutters, page_width, side_bands, doc,
        )
        blocks = _insert_rotated_captions(blocks, _collect_rotated_caption_blocks(doc))
        # 跨列拆分后，原先被混合块隔开的同栏正文碎片会重新相邻；先并回
        # 完整段落再翻译，否则一句话会被切成几截分别送模型。
        blocks = _merge_body_paragraph_fragments(blocks, body_size)
        blocks = _strip_front_citation_noise(blocks, side_bands)
        blocks = _clip_text_rects_to_bbox(blocks)
        blocks = _drop_covered_blocks(blocks)
        if _DIAG_PATH:
            diag_local = _local_edges(ordered)
            _diag_dump({
                "body_size": body_size,
                "has_gutter": has_gutter,
                "gutters": {str(k): v for k, v in gutters.items()},
                "tables": {
                    str(k): [
                        [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]
                        for r in v
                    ]
                    for k, v in table_rects.items()
                },
                "lines": [
                    {
                        "page": ln.page, "col": ln.col,
                        "x0": round(ln.x0, 2), "x1": round(ln.x1, 2),
                        "y0": round(ln.y0, 2), "y1": round(ln.y1, 2),
                        "size": round(ln.size, 2), "bold": ln.bold, "math": ln.math,
                        "margin": ln.is_margin, "text": ln.text[:80],
                    }
                    for ln in ordered
                ],
                "edges": [
                    [round(e[0], 2), round(e[1], 2)] if e else None
                    for e in (diag_local.get(id(ln)) for ln in ordered)
                ],
                "blocks": [
                    {
                        "page": b.page, "kind": b.kind, "len": len(b.text),
                        "bbox": [round(v, 2) for v in b.bbox],
                        "text": b.text[:80],
                    }
                    for b in blocks
                ],
            })
        blocks = _merge_front_matter_cards(blocks, front, page_width)
        blocks = _merge_heading_continuations(blocks)
        blocks = _attach_equation_numbers(blocks, doc)
        blocks = _merge_affiliations(blocks)
        blocks = _drop_noise(blocks)
        blocks = _drop_equation_orphans(blocks)
        blocks = _merge_across_pages(blocks, table_rects)
        blocks = _merge_front_continuations(blocks)
        blocks = _merge_caption_continuations(blocks, table_rects)
        blocks = _merge_small_caption_continuations(blocks, body_size)

        if with_figures and figure_dir is not None:
            out = Path(figure_dir)
            if paper_id:
                out = out / paper_id
            blocks = _extract_ruled_algorithms(doc, blocks, out / "tables")
            blocks = _extract_tables(doc, blocks, out / "tables", detected)
            blocks = _absorb_table_notes(blocks, body_size)
            blocks = _extract_figures(doc, blocks, out / "figures", body_size, detected)
            # 区域层的图区作为「带标注矢量图」的候选来源：图内标注已被建块
            # 阶段剔出正文流，原路径的文字生长失效，改由区域层提供图区，
            # 图注锚定负责配对与几何校验（图不压正文/表格）。
            blocks = _extract_vector_text_figures(
                doc, blocks, out / "figures", body_size, page_regions,
            )
            blocks = _collapse_vector_figure_pages(
                doc, blocks, out / "figures", body_size, page_regions,
            )
            blocks = _drop_duplicate_figures(blocks)
            blocks = _drop_float_text_fragments(blocks, body_size)
            # 图表裁好之后再清一次：图里的坐标轴/标注不该再作为公式出现
            blocks = _drop_equations_inside_floats(blocks)
            blocks = _crop_equations(doc, blocks, out / "equations")
            blocks = _normalize_block_inline_math(blocks)
            blocks = _crop_inline_math(doc, blocks, out / "inline_math")
        else:
            blocks = _normalize_block_inline_math(blocks)
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
