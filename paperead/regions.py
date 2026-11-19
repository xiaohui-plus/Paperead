"""页面语义区域分类。

PDF 里没有「段落」「图区」这类对象，只有画在坐标上的字形。此前所有判断
都发生在「建块之后」——先用逐行几何规则拼出段落块，再回头从块里猜哪些
是图、哪些是表格。这带来一个绕不开的循环依赖：

- 图区要不要裁进正文，取决于「哪里有正文块」；
- 正文块有没有被图吞掉，又取决于「图区在哪」。

只要中间一步判错（图内短标注被当成正文、正文被拆成短块），错误就会在
两个阶段之间来回放大：段落拆碎 → 页面没有长正文块 → 整页图化的守卫失效
→ 整页被当切片。

这里把「区域」提到建块之前：先按页面几何把正文区、图区、表格区、行号栏
分开，再让建块只看属于正文的行。本模块只做几何与统计，不依赖 layout.py，
避免循环导入；判定用的文本回调由调用方注入。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol, Sequence


class LineLike(Protocol):
    """layout.Line 的最小字段协议（避免 regions 反向依赖 layout）。"""

    text: str
    bbox: tuple[float, float, float, float]
    page: int
    col: int
    size: float
    math: bool
    is_margin: bool


@dataclass(frozen=True)
class Region:
    """页面上的一个矩形语义区域。kind 取 figure/table/line_number。"""

    kind: str
    page: int
    bbox: tuple[float, float, float, float]
    source: str = "geometry"

    @property
    def area(self) -> float:
        x0, y0, x1, y1 = self.bbox
        return max(0.0, x1 - x0) * max(0.0, y1 - y0)

    def cover_ratio(self, inner: Sequence[float]) -> float:
        """inner 矩形被本区域覆盖的比例（判断「这个块落在图里」）。"""
        box = max(0.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
        if box <= 0:
            return 0.0
        return _inter_area(self.bbox, inner) / box

    def overlap_ratio(self, other: Sequence[float]) -> float:
        """两区域交集占较小者的比例（判断「同一块区域」）。"""
        a = max(0.0, (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1]))
        b = max(0.0, (other[2] - other[0]) * (other[3] - other[1]))
        small = min(a, b)
        if small <= 0:
            return 0.0
        return _inter_area(self.bbox, other) / small

    def gap(self, other: Sequence[float]) -> float:
        """两矩形的最近边距（相交为 0）。"""
        return _rect_gap(self.bbox, other)


@dataclass
class PageRegions:
    """单页的区域分类结果。"""

    page: int
    width: float
    height: float
    figures: list[Region] = field(default_factory=list)
    tables: list[Region] = field(default_factory=list)
    line_numbers: list[Region] = field(default_factory=list)

    def figure_at(self, bbox: Sequence[float], threshold: float = 0.6) -> Region | None:
        for r in self.figures:
            if r.cover_ratio(bbox) >= threshold:
                return r
        return None

    def table_at(self, bbox: Sequence[float], threshold: float = 0.5) -> Region | None:
        for r in self.tables:
            if r.cover_ratio(bbox) >= threshold:
                return r
        return None

    def is_line_number(self, bbox: Sequence[float], threshold: float = 0.6) -> bool:
        return any(r.cover_ratio(bbox) >= threshold for r in self.line_numbers)

    @property
    def has_floats(self) -> bool:
        return bool(self.figures)


def _inter_area(a: Sequence[float], b: Sequence[float]) -> float:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def _rect_gap(a: Sequence[float], b: Sequence[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return (dx * dx + dy * dy) ** 0.5


_LINE_NUMBER_RE = re.compile(r"^\d{1,3}$")


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def detect_line_numbers(
    lines: Sequence[LineLike],
    body_size: float,
) -> list[Region]:
    """识别 arXiv 一类的侧边行号（每 5 行一个数字，贴栏左缘）。

    行号不是正文，但它是「数字 + 小字号 + 独立一行」，会被断段规则当成
    一个短段落，随后又被并进上一行，最终在正文里留下 `... and achieves the`
    `best performance on both Max Vina Dock and 4` 这种夹着数字的句子。
    必须在建块前整行剔除。

    只认三条同时成立的强证据，避免误删表格单元格和公式编号：
    文本是 1~3 位纯数字、整行落在栏左缘外侧、纵向间隔近似等距且至少 3 个。
    """
    out: list[Region] = []
    groups: dict[tuple[int, int], list[LineLike]] = {}
    for ln in lines:
        if ln.is_margin or ln.col < 0 or not ln.text.strip():
            continue
        groups.setdefault((ln.page, ln.col), []).append(ln)

    for (page, _col), group in groups.items():
        candidates = [
            ln for ln in group
            if _LINE_NUMBER_RE.match(ln.text.strip())
            and 0 < ln.size <= max(body_size * 1.02, 1.0)
        ]
        if len(candidates) < 3:
            continue
        rest = [ln for ln in group if ln not in candidates]
        if not rest:
            continue
        # 栏左缘取 10 分位而不是最小值：段落首行缩进、公式悬挂缩进都会让
        # 个别行起点更靠左，取最小值会把行号判到「栏内」而漏检。
        col_left = _percentile([ln.bbox[0] for ln in rest], 0.1)
        candidates = [ln for ln in candidates if ln.bbox[2] <= col_left + 8]
        if len(candidates) < 3:
            continue
        ys = sorted(ln.bbox[1] for ln in candidates)
        gaps = [b - a for a, b in zip(ys, ys[1:])]
        if not gaps or statistics.mean(gaps) <= 0:
            continue
        # 行号等距出现；正文里的数字（表格、列表编号）间距杂乱，过不了这一关。
        if statistics.pstdev(gaps) / statistics.mean(gaps) > 0.45:
            continue
        for ln in candidates:
            out.append(Region("line_number", page, tuple(ln.bbox), "line_number"))
    return out


def _merge_boxes(
    boxes: Iterable[Sequence[float]],
    gap_x: float,
    gap_y: float,
) -> list[tuple[float, float, float, float]]:
    merged: list[tuple[float, float, float, float]] = []
    for box in boxes:
        cur = (box[0], box[1], box[2], box[3])
        for i, exist in enumerate(merged):
            if (
                max(cur[0], exist[0]) - min(cur[2], exist[2]) <= gap_x
                and max(cur[1], exist[1]) - min(cur[3], exist[3]) <= gap_y
            ):
                merged[i] = (
                    min(cur[0], exist[0]), min(cur[1], exist[1]),
                    max(cur[2], exist[2]), max(cur[3], exist[3]),
                )
                break
        else:
            merged.append(cur)
    return merged


def _is_body_like(ln: LineLike, body_size: float) -> bool:
    """成段正文：够长 + 正文同字号 + 不是公式行。

    这是图区扩张的边界。不能用「短就是图内标注」的逆命题——图内说明性
    标注本身就可能很长，只有「长 + 正文字号」才算正文。
    """
    text = ln.text.strip()
    if len(text) < 220:
        return False
    if ln.math:
        return False
    return body_size <= 0 or ln.size >= body_size * 0.95


def trim_off_body(
    box: tuple[float, float, float, float],
    lines: Sequence[LineLike],
    body_size: float,
) -> tuple[float, float, float, float] | None:
    """图区把成段正文框进来时，纵向收缩到正文之外。

    在建块前做这一步，才能打破「图区依赖正文块、正文块又依赖图区」的
    循环。收缩后高度不足 60pt 说明候选区本身就是正文，直接丢弃。
    """
    out = list(box)
    for ln in lines:
        if not _is_body_like(ln, body_size):
            continue
        br = ln.bbox
        # 只看真正被图区覆盖到一半以上的正文行
        inter = _inter_area(br, out)
        area = max(1e-6, (br[2] - br[0]) * (br[3] - br[1]))
        if inter / area < 0.5:
            continue
        if (br[1] + br[3]) / 2 < (out[1] + out[3]) / 2:
            out[1] = max(out[1], br[3] + 2)
        else:
            out[3] = min(out[3], br[1] - 2)
    if out[3] - out[1] < 60 or out[2] - out[0] < 60:
        return None
    return (out[0], out[1], out[2], out[3])


def detect_float_regions(
    page,
    page_no: int,
    lines: Sequence[LineLike],
    body_size: float,
    tables: Sequence[Region] = (),
    is_caption: Callable[[str], bool] | None = None,
) -> list[Region]:
    """识别「图区」候选：位图区域 + 带文字标注的矢量簇。

    和旧的 `_figure_rects` 最大的区别是：不再要求矢量簇「区域内没有
    文字」。期刊插图几乎都带坐标轴、图例、子图标题，按「无文字」筛选
    会把绝大多数图漏掉，只能退化成靠图注锚定；而图注一旦被并进正文，
    整张图就消失、图内文字反而留在正文流里。

    这里改成「簇内短标注占比高 + 没有成段正文 + 不是公式块」就收，
    并用 `trim_off_body` 把偶然被框进来的正文行剔出去。
    """
    regions: list[Region] = []
    try:
        page_area = page.rect.width * page.rect.height
    except Exception:
        return regions

    try:
        infos = [tuple(info["bbox"]) for info in page.get_image_info()]
    except Exception:
        infos = []
    for box in _merge_boxes(
        [b for b in infos if (b[2] - b[0]) * (b[3] - b[1]) > 500 and (b[3] - b[1]) > 10],
        gap_x=30.0,
        gap_y=30.0,
    ):
        if (box[2] - box[0]) * (box[3] - box[1]) > 8000 and (box[3] - box[1]) > 40:
            regions.append(Region("figure", page_no, box, "bitmap"))

    try:
        clusters = [tuple(r) for r in page.cluster_drawings()]
    except Exception:
        clusters = []

    for box in _merge_boxes(clusters, gap_x=24.0, gap_y=24.0):
        width = box[2] - box[0]
        height = box[3] - box[1]
        area = width * height
        if area < 4000 or height <= 40 or area > page_area * 0.7:
            continue
        if any(t.overlap_ratio(box) > 0.3 for t in tables):
            continue
        inside = [
            ln for ln in lines
            if box[0] <= (ln.bbox[0] + ln.bbox[2]) / 2 <= box[2]
            and box[1] <= (ln.bbox[1] + ln.bbox[3]) / 2 <= box[3]
            and ln.text.strip()
            and not ln.is_margin
        ]
        if not inside:
            # 纯图形（热图、示意图）：没有文字就没有误伤正文的风险，
            # 但仍要求足够大，避免把分隔线、页眉横线当图。
            if area > 9000:
                regions.append(Region("figure", page_no, box, "vector"))
            continue
        # 对齐/多行公式的矢量簇形状和插图很像（多行短文本 + 大量线条），
        # 数学字体行占多数时必须否决，否则公式会被整块裁成图片。
        if sum(1 for ln in inside if ln.math) >= len(inside) * 0.5:
            continue
        if sum(1 for ln in inside if _is_body_like(ln, body_size)) > 0:
            continue
        caption_count = sum(1 for ln in inside if is_caption and is_caption(ln.text))
        short = sum(1 for ln in inside if len(ln.text.strip()) <= 70)
        # 图内短标注为主：至少 3 行且 70% 是短行；或「图注 + 少量标注」的
        # 小型示意图（2 行以内、含图注）。
        if len(inside) >= 3 and short >= len(inside) * 0.7:
            regions.append(Region("figure", page_no, box, "vector_labels"))
        elif len(inside) <= 2 and caption_count > 0:
            regions.append(Region("figure", page_no, box, "vector_caption"))

    merged: list[Region] = []
    for region in regions:
        for i, exist in enumerate(merged):
            if (
                region.overlap_ratio(exist.bbox) > 0.2
                or region.gap(exist.bbox) <= 24
            ):
                merged[i] = Region(
                    "figure", page_no,
                    (
                        min(region.bbox[0], exist.bbox[0]),
                        min(region.bbox[1], exist.bbox[1]),
                        max(region.bbox[2], exist.bbox[2]),
                        max(region.bbox[3], exist.bbox[3]),
                    ),
                    exist.source,
                )
                break
        else:
            merged.append(region)

    out: list[Region] = []
    for region in merged:
        trimmed = trim_off_body(region.bbox, lines, body_size)
        if trimmed is None:
            continue
        out.append(Region("figure", page_no, trimmed, region.source))
    return out


def build_page_regions(
    doc,
    lines: Sequence[LineLike],
    body_size: float,
    table_rects: dict[int, list] | None = None,
    is_caption: Callable[[str], bool] | None = None,
) -> dict[int, PageRegions]:
    """按页建立区域分类。lines 需已按页分组前的全量行（含页码信息）。"""
    by_page: dict[int, list[LineLike]] = {}
    for ln in lines:
        if ln.text.strip():
            by_page.setdefault(ln.page, []).append(ln)

    out: dict[int, PageRegions] = {}
    for page_no in range(1, len(doc) + 1):
        page = doc[page_no - 1]
        result = PageRegions(page=page_no, width=page.rect.width, height=page.rect.height)
        for item in (table_rects or {}).get(page_no, []):
            rect = item[0] if isinstance(item, tuple) else item
            result.tables.append(Region("table", page_no, tuple(rect), "table"))
        page_lines = by_page.get(page_no, [])
        result.line_numbers = detect_line_numbers(page_lines, body_size)
        result.figures = detect_float_regions(
            page, page_no, page_lines, body_size, result.tables, is_caption,
        )
        out[page_no] = result
    return out