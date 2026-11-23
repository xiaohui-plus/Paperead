"""页内段落流（P0）：把解析块整理成「按页、按栏、按段落」的语义流。

这一层不渲染、不碰 PDF，只回答三个问题：

1. 这一页是单栏 / 双栏 / 双栏+侧栏；
2. 每个块属于哪一栏（左 / 右 / 通栏 / 侧栏）——栏归属一旦定下，排版层不得再改；
3. 哪些相邻块其实是同一个段落，以及跨页块在本页应该排哪一段文字。

排版层（P1）只消费本层的 ``Paragraph``：段落的栏归属、锚点、段内文本/公式序列
都在这里定死，避免下游再用阈值猜「这段属于哪一栏」。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .textnorm import apply_symbol_fallback

# 参与栏结构判定的块：图/表/公式是浮动元素，不决定栏骨架。
FLOW_KINDS = frozenset({"para", "abstract", "text", "caption", "heading", "title"})
# 允许并成一段的块类型：标题、图注、图/表/公式各自独立成段。
MERGEABLE_KINDS = frozenset({"para", "abstract", "text"})
# 句末标点：前块以这些收尾才算「句子写完」，否则视为被解析切开的同段。
_SENTENCE_END = re.compile(r"[.!?。！？]\s*$")
_SENTENCE_SPLIT = re.compile(r"[.!?。！？]")
# 译文里混进的私用区字形与方框：仿宋没有对应字形，不清理会写成实心方块。
_BAD_GLYPH_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ue000-\uf8ff\ufffd\u25a1\u25a0\u25ae\u25af]")
# 数学字母区（𝑖/𝑣/𝑥/𝑂 等）：仿宋同样没有字形，必须归一成 ASCII 或希腊字母。
_MATH_ALNUM_RE = re.compile(r"[\U0001D400-\U0001D7FF]")
_GREEK_FIX = {"ϕ": "φ", "ϵ": "ε", "ϖ": "π", "ϰ": "κ"}


def _ascii_math(text: str) -> str:
    return _MATH_ALNUM_RE.sub(
        lambda m: unicodedata.normalize("NFKC", m.group(0)), text
    )


def _clean_flow_text(text: str, items: list[dict], fragmented: bool) -> str:
    """清掉译文里无法渲染的字形；碎片化块再删掉多余的单字符公式符号。

    不可渲染的三类：私用区/替换符/方框、数学字母区、缺字形的希腊变体。
    碎片化块的公式区已原样保留，译文里同一批符号若照抄会散落在中文句子中。
    """
    if fragmented:
        # 碎片化块的公式区已原样保留，译文里的数学字母/私用区字形都是残渣。
        # 不能归一成 ASCII 再保留：`𝑖`→`i`、`𝜙`→`ϕ` 会在中文句子里留下孤立
        # 拉丁字母，看起来就是「符号没清干净」。
        value = _BAD_GLYPH_RE.sub("", text)
        value = _MATH_ALNUM_RE.sub("", value)
        for item in items:
            core = _bad_free(item)
            if core:
                value = value.replace(core, "")
        value = apply_symbol_fallback(value)
        return re.sub(r"[ \t]{2,}", " ", value).strip()
    value = _BAD_GLYPH_RE.sub(" ", text)
    value = _ascii_math(value)
    for bad, good in _GREEK_FIX.items():
        value = value.replace(bad, good)
    # 与渲染层同一张缺字形替换表：排版按替换后的字宽计算，写入时才不会
    # 「∈」变「属于」后挤进下一行/下一列（实测会造成中文行重叠）。
    value = apply_symbol_fallback(value)
    return re.sub(r"[ \t]{2,}", " ", value)


def _bad_free(item: dict) -> str:
    """公式 source 去掉 PUA/方框并归一后，长度 ≤4 才值得从译文里删。"""
    raw = str(item.get("source") or "")
    core = re.sub(r"\s+", "", _ascii_math(_BAD_GLYPH_RE.sub("", raw)))
    if len(core) < 2 or len(core) > 4:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in core):
        return ""
    return core

COLUMN_SIDEBAR = -2
COLUMN_SPAN = -1
_CROSS_COLUMN_REACH = 6.0
_MIN_GUTTER_RATIO = 0.12
_MIN_BLOCKS_PER_COLUMN = 3
_MIN_OVERLAP_RATIO = 0.6
_MAX_PARAGRAPH_GAP_RATIO = 1.6


@dataclass(frozen=True)
class PageSkeleton:
    """一页的版式骨架：栏区间与侧栏区间都来自原文几何，不重新计算。"""

    page: int
    width: float
    height: float
    columns: tuple[tuple[float, float], ...]
    sidebar: tuple[float, float] | None = None
    layout: str = "single"

    @property
    def is_dual(self) -> bool:
        return len(self.columns) == 2

    @property
    def body_left(self) -> float:
        return self.columns[0][0]

    @property
    def body_right(self) -> float:
        return self.columns[-1][1]

    @property
    def gutter(self) -> tuple[float, float] | None:
        if not self.is_dual:
            return None
        return (self.columns[0][1], self.columns[1][0])


@dataclass
class Segment:
    """段内序列的一个原子：文字 / 行内公式 / 行间公式。"""

    type: str
    value: str
    math_key: int = -1
    latex: str = ""
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class Paragraph:
    """一个逻辑段落：位置锚定原文段落，内部由 segment 流组成。"""

    id: str
    page: int
    column: int
    kind: str
    level: int
    anchor: tuple[float, float, float, float]
    segments: list[Segment] = field(default_factory=list)
    source_blocks: list[str] = field(default_factory=list)
    cross_page_group: str = ""
    translatable: bool = True

    @property
    def text(self) -> str:
        return "".join(
            seg.value if seg.type == "text" else seg.value
            for seg in self.segments
        )


@dataclass
class PageFlow:
    page: int
    skeleton: PageSkeleton
    paragraphs: list[Paragraph]


def _bbox(block: dict) -> tuple[float, float, float, float]:
    raw = block.get("bbox") or (0.0, 0.0, 0.0, 0.0)
    return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))


def _center_x(block: dict) -> float:
    x0, _, x1, _ = _bbox(block)
    return (x0 + x1) / 2


def _size(block: dict) -> float:
    try:
        value = float(block.get("size") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else 10.0


def _find_gutter(flowing: list[dict]) -> tuple[float, float] | None:
    """在内容范围内找「没有任何块覆盖」的最宽纵向空白带，即栏缝。

    按块中心二分容易被「跨中线的宽块」带偏（实测第 11 页把栏缝压成 0 宽，
    宽块随后被误判成通栏）；按 x 投影的零覆盖段找缝则只依赖几何，宽块只会
    让缝变窄，不会让缝消失。
    """
    if not flowing:
        return None
    lo = int(min(_bbox(block)[0] for block in flowing))
    hi = int(max(_bbox(block)[2] for block in flowing))
    if hi - lo < 40:
        return None
    bins = [0] * (hi - lo + 2)
    for block in flowing:
        left = max(lo, int(_bbox(block)[0]))
        right = min(hi, int(_bbox(block)[2]))
        for x in range(left, right + 1):
            bins[x - lo] += 1
    runs: list[tuple[int, int]] = []
    start = None
    for index, count in enumerate(bins):
        if count == 0 and start is None:
            start = index
        elif count > 0 and start is not None:
            runs.append((start + lo, index - 1 + lo))
            start = None
    if start is not None:
        runs.append((start + lo, len(bins) - 1 + lo))
    inner = [run for run in runs if run[0] > lo + 2 and run[1] < hi - 2]
    if not inner:
        return None
    return max(inner, key=lambda run: run[1] - run[0])


def infer_skeleton(
    page_blocks: list[dict],
    page_width: float,
    page_height: float,
    page: int,
    gutter_center: float | None = None,
) -> PageSkeleton:
    """从块几何推断单栏 / 双栏 / 双栏+侧栏。

    判据选的是「块中心 x 的一维二分」而不是 x0 频次：双栏论文左右栏的
    段首经常对齐到同一 x0，按 x0 会误判成单栏；按中心则形成两个明显峰。
    """
    flowing = [
        block for block in page_blocks
        if block.get("kind") in FLOW_KINDS
        and block.get("column_role") != "sidebar"
    ]
    side = [
        block for block in page_blocks
        if block.get("kind") in FLOW_KINDS
        and block.get("column_role") == "sidebar"
    ]

    sidebar: tuple[float, float] | None = None
    if side:
        sx0 = min(_bbox(block)[0] for block in side)
        sx1 = max(_bbox(block)[2] for block in side)
        if 8.0 < sx1 - sx0 < page_width * 0.34:
            sidebar = (sx0, sx1)

    if not flowing:
        left = sidebar[0] if sidebar else page_width * 0.1
        right = page_width * 0.9
        return PageSkeleton(
            page=page,
            width=page_width,
            height=page_height,
            columns=((left, right),),
            sidebar=sidebar,
            layout="single",
        )

    x0_max = max(_bbox(block)[2] for block in flowing)
    x0_min = min(_bbox(block)[0] for block in flowing)
    # 解析层已经用原页词坐标算过栏缝时直接采用：块几何会被跨列块与稀疏页
    # 带偏（实测第 2/11/15 页块投影找不到缝，实际栏缝都在 x≈297），栏缝
    # 必须以原页词为准，块只用来判断左右归属。
    if gutter_center is not None and page_width * 0.2 < gutter_center < page_width * 0.8:
        g0, g1 = gutter_center - 2.0, gutter_center + 2.0
        left_count = sum(1 for block in flowing if _center_x(block) < g0)
        right_count = sum(1 for block in flowing if _center_x(block) >= g1)
        if left_count >= _MIN_BLOCKS_PER_COLUMN and right_count >= _MIN_BLOCKS_PER_COLUMN:
            return PageSkeleton(
                page=page,
                width=page_width,
                height=page_height,
                columns=((x0_min, g0), (g1, x0_max)),
                sidebar=sidebar,
                layout="dual_sidebar" if sidebar else "dual",
            )
    gutter = _find_gutter(flowing)
    left_count = right_count = 0
    if gutter is not None:
        left_count = sum(1 for block in flowing if _center_x(block) < gutter[0])
        right_count = sum(1 for block in flowing if _center_x(block) >= gutter[1])

    if (
        gutter is not None
        and gutter[1] - gutter[0] >= 8.0
        and left_count >= _MIN_BLOCKS_PER_COLUMN
        and right_count >= _MIN_BLOCKS_PER_COLUMN
        and gutter[0] - x0_min >= page_width * 0.15
        and x0_max - gutter[1] >= page_width * 0.15
    ):
        columns = ((x0_min, gutter[0]), (gutter[1], x0_max))
        layout = "dual_sidebar" if sidebar else "dual"
    else:
        columns = ((x0_min, x0_max),)
        layout = "single"

    return PageSkeleton(
        page=page,
        width=page_width,
        height=page_height,
        columns=columns,
        sidebar=sidebar,
        layout=layout,
    )


def assign_column(block: dict, skeleton: PageSkeleton) -> int:
    """块栏归属：0 左栏 / 1 右栏 / -1 通栏 / -2 侧栏。"""
    if block.get("column_role") == "sidebar":
        return COLUMN_SIDEBAR
    x0, _, x1, _ = _bbox(block)
    if skeleton.sidebar and x0 >= skeleton.sidebar[0] - 2 and x1 <= skeleton.sidebar[1] + 3:
        return COLUMN_SIDEBAR
    if skeleton.is_dual:
        gutter = skeleton.gutter
        assert gutter is not None
        left_reach = min(x1, gutter[0]) - x0
        right_reach = x1 - max(x0, gutter[1])
        if left_reach > _CROSS_COLUMN_REACH and right_reach > _CROSS_COLUMN_REACH:
            return COLUMN_SPAN
        mid = (gutter[0] + gutter[1]) / 2
        return 0 if _center_x(block) < mid else 1
    # 单栏页只有一个「通栏」栏位；若页面另有侧栏，侧栏已在上面分流。
    return COLUMN_SPAN


def split_text_by_pages(text: str, shares: list[float]) -> list[str]:
    """按页占比切分跨页块文本，保证 ``''.join(结果) == text``。

    切点优先落在句末标点之后（目标比例 ±20% 窗口内取最近），找不到就按字符
    比例硬切。硬切至少不会丢字，句切则让下页从整句开头读起。
    """
    if not text:
        return ["" for _ in shares]
    if len(shares) == 1:
        return [text]
    total = sum(max(share, 0.0) for share in shares) or float(len(shares))
    weights = [max(share, 0.0) / total for share in shares]

    cuts: list[int] = []
    cursor = 0
    consumed = 0.0
    for weight in weights[:-1]:
        consumed += weight
        target = int(round(consumed * len(text)))
        window = max(8, int(len(text) * 0.2))
        low = max(cursor + 1, target - window)
        high = min(len(text) - 1, target + window)
        cut = None
        for match in _SENTENCE_SPLIT.finditer(text, low, high + 1):
            cut = match.end()
        if cut is None:
            cut = max(cursor + 1, min(target, len(text) - 1))
        cut = max(cut, cursor + 1)
        cuts.append(cut)
        cursor = cut

    parts: list[str] = []
    start = 0
    for cut in cuts:
        parts.append(text[start:cut])
        start = cut
    parts.append(text[start:])
    return parts


def _segments_from_block(block: dict, text: str | None = None) -> list[Segment]:
    """把块的 text 按 inline_math 偏移切成 文字/公式 交替序列。"""
    source = block.get("text") or "" if text is None else text
    math_items = block.get("inline_math") or []
    usable = []
    for index, item in enumerate(math_items):
        try:
            start = int(item.get("start", -1))
            end = int(item.get("end", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= start < end <= len(source):
            usable.append((start, end, index, item))
    if not usable:
        return [Segment(type="text", value=source)] if source else []

    usable.sort()
    segments: list[Segment] = []
    cursor = 0
    for start, end, index, item in usable:
        if start < cursor:
            continue
        if start > cursor:
            segments.append(Segment(type="text", value=source[cursor:start]))
        raw_bbox = item.get("bbox")
        math_bbox = (
            tuple(float(v) for v in raw_bbox)
            if raw_bbox and len(raw_bbox) >= 4
            else None
        )
        segments.append(
            Segment(
                type="math",
                value=source[start:end],
                math_key=index,
                latex=str(item.get("latex") or ""),
                bbox=math_bbox,
            )
        )
        cursor = end
    if cursor < len(source):
        segments.append(Segment(type="text", value=source[cursor:]))
    return segments


def _inline_math_fragmented(items: list[dict]) -> bool:
    """行内公式是否被解析成单字符碎片。

    解析层常把一个公式拆成「ε / × / σ」多个 token；逐 token 内联会让符号
    散落在中文句子中间。碎片化块不改写公式，交给页面级禁区原样保留。
    """
    if not items:
        return False
    if len(items) > 4:
        return True
    for item in items:
        bbox = item.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        if (bbox[2] - bbox[0]) < 14.0 and (bbox[3] - bbox[1]) < 11.5:
            return True
    return False


def _loose_index(text: str) -> tuple[str, list[int]]:
    """去空白 + NFKC 归一后的文本，以及每个字符回原文的下标。

    译文清洗会把数学字母区归一成 ASCII（``𝑢1``→``u1``），而 ``inline_math``
    的 ``source`` 仍是原 Unicode。若直接用原串 ``find`` 定位，译文里的每个
    公式都会失配，最终公式当普通文本写进 PDF —— 字体缺字形就是 ``\\x00``
    黑块。定位必须和清洗用同一套归一化。
    """
    chars: list[str] = []
    index: list[int] = []
    for position, ch in enumerate(text):
        if ch.isspace():
            continue
        # NFKC 可能把一个字符展开成多个（ﬁ→fi），必须逐输出字符记录同一
        # 原下标，否则 index_map 与 haystack 长度不一致、切片越界。
        for norm in unicodedata.normalize("NFKC", ch):
            chars.append(norm)
            index.append(position)
    return "".join(chars), index


def _loose_token(token: str) -> str:
    """公式 source 的归一化键：与 ``_clean_flow_text`` 同一口径。

    ``source`` 常带私用区花体（``\\ue241 = {𝑢1…}``），译文清洗时这些字符
    被删掉。若定位键保留 PUA，永远匹配不上，公式就会退化成普通文本。
    """
    token = _BAD_GLYPH_RE.sub("", token or "")
    token = apply_symbol_fallback(token)
    return "".join(
        unicodedata.normalize("NFKC", ch) for ch in token if not ch.isspace()
    )


def _segments_from_translation(block: dict) -> list[Segment]:
    """按译文切 segment：公式 token 用原文 ``source`` 在译文里定位。

    旧译文常常保留公式原样（``u_{AE}`` 这种），也可能把公式展开成中文描述。
    定位用「去空白 + NFKC」口径（与 ``_clean_flow_text`` 一致），失败就只保留
    文字段落并把公式记入缺失（排版层不会把公式插到句外）。
    """
    items = block.get("inline_math") or []
    fragmented = _inline_math_fragmented(items)
    translated = _clean_flow_text(block.get("translated") or "", items, fragmented)
    source = block.get("text") or ""
    if not translated.strip():
        return _segments_from_block(block, source)
    if fragmented:
        return [Segment(type="text", value=translated)]

    haystack, index_map = _loose_index(translated)
    segments: list[Segment] = []
    cursor = 0
    search_from = 0
    for index, item in enumerate(items):
        token = str(item.get("source") or "")
        if not token:
            try:
                token = source[int(item.get("start", -1)):int(item.get("end", -1))]
            except (TypeError, ValueError):
                continue
        key = _loose_token(token)
        if not key:
            continue
        loose_pos = haystack.find(key, search_from)
        if loose_pos < 0:
            continue
        start = index_map[loose_pos]
        end = index_map[loose_pos + len(key) - 1] + 1
        if start < cursor:
            continue
        if start > cursor:
            segments.append(Segment(type="text", value=translated[cursor:start]))
        raw_bbox = item.get("bbox")
        math_bbox = (
            tuple(float(v) for v in raw_bbox)
            if raw_bbox and len(raw_bbox) >= 4
            else None
        )
        segments.append(
            Segment(
                type="math",
                value=translated[start:end],
                math_key=index,
                latex=str(item.get("latex") or ""),
                bbox=math_bbox,
            )
        )
        cursor = end
        search_from = loose_pos + len(key)
    if cursor < len(translated):
        segments.append(Segment(type="text", value=translated[cursor:]))
    return segments or [Segment(type="text", value=translated)]


def _segments_for_block(block: dict) -> list[Segment]:
    """有译文排译文，没有译文排原文（由排版层决定是否跳过）。"""
    return _segments_from_translation(block)


def _x_overlap_ratio(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    inter = min(a[2], b[2]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    narrow = max(1.0, min(a[2] - a[0], b[2] - b[0]))
    return inter / narrow


def _can_continue(prev: dict, nxt: dict, prev_anchor, next_anchor) -> bool:
    if prev.get("kind") not in MERGEABLE_KINDS or nxt.get("kind") not in MERGEABLE_KINDS:
        return False
    if _SENTENCE_END.search((prev.get("text") or "").rstrip()):
        return False
    if _x_overlap_ratio(prev_anchor, next_anchor) < _MIN_OVERLAP_RATIO:
        return False
    gap = next_anchor[1] - prev_anchor[3]
    limit = _size(prev) * _MAX_PARAGRAPH_GAP_RATIO
    return -limit <= gap <= limit


def merge_page_paragraphs(
    page_blocks: list[dict],
    skeleton: PageSkeleton,
    page: int,
) -> list[Paragraph]:
    """同页同栏的断句碎片并回一段；标题/图注/公式各自独立。"""
    ordered = sorted(
        page_blocks,
        key=lambda b: (
            assign_column(b, skeleton),
            _bbox(b)[1],
            _bbox(b)[0],
        ),
    )
    paragraphs: list[Paragraph] = []
    for block in ordered:
        column = assign_column(block, skeleton)
        anchor = _bbox(block)
        kind = str(block.get("kind") or "para")
        group = str(block.get("_cross_page_group") or "")
        # 侧栏是逐行条目（Edited by / Reviewed by / 单位），两行是两条信息，
        # 合并会把不同条目串成一句，所以侧栏块不参与段落合并。
        if paragraphs and kind in MERGEABLE_KINDS and column != COLUMN_SIDEBAR:
            last = paragraphs[-1]
            if (
                last.column == column
                and last.kind in MERGEABLE_KINDS
                and last.cross_page_group == group
                and _can_continue(
                    {"kind": last.kind, "text": last.text, "size": last.anchor[3] - last.anchor[1]},
                    {"kind": kind, "text": block.get("text") or "", "size": _size(block)},
                    last.anchor,
                    anchor,
                )
            ):
                last.segments.extend(_segments_for_block(block))
                last.translatable = last.translatable or bool(
                    (block.get("translated") or "").strip()
                )
                last.anchor = (
                    min(last.anchor[0], anchor[0]), min(last.anchor[1], anchor[1]),
                    max(last.anchor[2], anchor[2]), max(last.anchor[3], anchor[3]),
                )
                last.source_blocks.append(str(block.get("id") or ""))
                continue
        paragraphs.append(
            Paragraph(
                id=str(block.get("id") or ""),
                page=page,
                column=column,
                kind=kind,
                level=int(block.get("level") or 0),
                anchor=anchor,
                segments=_segments_for_block(block),
                source_blocks=[str(block.get("id") or "")],
                cross_page_group=group,
                translatable=bool((block.get("translated") or "").strip()),
            )
        )
    return paragraphs


def _expand_cross_page(blocks: list[dict]) -> list[dict]:
    """把跨页块按 ``bboxes`` 拆成每页一个片段。

    P0 只切文本（保证各页拼接等于全文）；行内公式归属留到 P1，跨页片段先
    按纯文本处理，避免半截公式落到某一页。
    """
    expanded: list[dict] = []
    for block in blocks:
        bboxes = block.get("bboxes") or []
        if len(bboxes) < 2:
            expanded.append(block)
            continue
        rows = sorted(bboxes, key=lambda row: int(row[0]))
        heights = [max(1.0, float(row[4]) - float(row[2])) for row in rows]
        pieces = split_text_by_pages(block.get("text") or "", heights)
        group = str(block.get("id") or "")
        for row, piece in zip(rows, pieces):
            clone = dict(block)
            clone["page"] = int(row[0])
            clone["bbox"] = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
            clone["text"] = piece
            clone["bboxes"] = []
            clone["inline_math"] = []
            clone["_cross_page_group"] = group
            expanded.append(clone)
    return expanded


def build_page_flow(
    blocks: list[dict],
    page_sizes: dict[int, tuple[float, float]],
    gutters: dict[int, float | None] | None = None,
) -> dict[int, PageFlow]:
    """把整篇块整理成「每页一套骨架 + 一串段落」。

    跨页块按 ``bboxes`` 拆到各页：每页一个段落片段，``cross_page_group`` 相同，
    文本按页高度占比切片，切片拼接严格等于原文。
    """
    by_page: dict[int, list[dict]] = {}
    for block in _expand_cross_page(blocks):
        try:
            page = int(block.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page <= 0:
            continue
        by_page.setdefault(page, []).append(block)

    flows: dict[int, PageFlow] = {}
    for page, page_blocks in sorted(by_page.items()):
        width, height = page_sizes.get(page, (595.0, 842.0))
        skeleton = infer_skeleton(
            page_blocks, width, height, page,
            gutter_center=(gutters or {}).get(page),
        )
        flows[page] = PageFlow(
            page=page,
            skeleton=skeleton,
            paragraphs=merge_page_paragraphs(page_blocks, skeleton, page),
        )
    return flows


def assert_cross_page_conservation(
    blocks: list[dict],
    flows: dict[int, PageFlow],
) -> list[str]:
    """跨页块守恒断言：各页片段文本拼接必须严格等于原块全文。

    返回问题列表，空列表表示全部守恒。只检查 ``len(bboxes) > 1`` 的块。
    """
    problems: list[str] = []
    for block in blocks:
        bboxes = block.get("bboxes") or []
        if len(bboxes) < 2:
            continue
        group = str(block.get("id") or "")
        pieces: list[str] = []
        for item in sorted(bboxes, key=lambda row: int(row[0])):
            page = int(item[0])
            flow = flows.get(page)
            if flow is None:
                problems.append(f"{group}: 缺第 {page} 页的段落流")
                continue
            found = [
                paragraph for paragraph in flow.paragraphs
                if paragraph.cross_page_group == group
            ]
            if not found:
                problems.append(f"{group}: 第 {page} 页没有对应片段")
                continue
            pieces.append("".join(paragraph.text for paragraph in found))
        if pieces and "".join(pieces) != (block.get("text") or ""):
            problems.append(f"{group}: 跨页片段拼接与原文不一致")
    return problems