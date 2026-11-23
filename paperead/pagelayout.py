"""栏内排版内核（P1）：把段落流排成行盒，不碰 PDF。

输入 ``PageFlow``（P0 产物），输出 ``LineBox`` 列表：

* 每行有精确的 x0/x1、基线、高度与每个 atom 的落点；
* 段落从自己的锚点顶部起排（方案 A：段内不拉伸、段后留白）；
* 首行缩进 2 字符、非段末行两端对齐；
* 行内公式是不可拆的 atom；图/表/行间公式是避让区，行盒不得与之相交。

这一层纯计算、可单测；P2 只负责把行盒和 atom 画到 PDF 上，不再自己判断
换行与对齐。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .pageflow import COLUMN_SIDEBAR, COLUMN_SPAN, PageFlow, Paragraph

SKIP_KINDS = frozenset({"reference", "figure", "table", "equation", "toc"})
# figure 不作文字禁排区：解析产物的 figure bbox 常把整条图注一起盖住，
# 当成禁排区会让 caption 永远排不进去（记忆里的 b0015 形态）。
OBSTACLE_KINDS = frozenset({"table", "equation"})

_CJK = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")
_TOKEN = re.compile(
    r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]"
    r"|[A-Za-z0-9][A-Za-z0-9'’\-\.]*"
    r"| +"
    r"|.",
    re.DOTALL,
)
# 禁则：这些标点不能出现在行首，排版时悬挂到上一行末尾。
_KINSOKU = "，。、）】》」』：；！？,.;:!?)"
_PAGE_BOTTOM_MARGIN = 40.0
_AVOID_STEP_LIMIT = 24


@dataclass(frozen=True)
class LayoutStyle:
    body_size: float = 9.5
    line_height: float = 1.15
    indent_chars: int = 2
    space_ratio: float = 0.28
    latin_ratio: float = 0.5

    @property
    def pitch(self) -> float:
        return self.body_size * self.line_height

    @property
    def indent(self) -> float:
        return self.body_size * self.indent_chars


@dataclass(frozen=True)
class Atom:
    type: str  # text | math
    value: str
    width: float
    math_key: int = -1
    latex: str = ""
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class PlacedAtom:
    atom: Atom
    x: float
    width: float


@dataclass
class LineBox:
    page: int
    column: int
    paragraph_id: str
    baseline: float
    y0: float
    y1: float
    x0: float
    x1: float
    justified: bool
    placed: list[PlacedAtom] = field(default_factory=list)

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


def _char_width(char: str, size: float, style: LayoutStyle) -> float:
    if _CJK.match(char):
        return size
    if char == " ":
        return size * style.space_ratio
    return size * style.latin_ratio


def tokenize_segments(segments, style: LayoutStyle) -> list[Atom]:
    """段内序列 → atom 流。中文逐字、拉丁按词、公式整体。"""
    size = style.body_size
    atoms: list[Atom] = []
    for segment in segments:
        if segment.type == "math":
            # 行内公式是原子：宽度按内容长度粗估，P2 用真实裁图/Latex 框替换。
            width = size * max(1.2, min(6.0, len(segment.value) * 0.35 or 1.2))
            atoms.append(
                Atom(
                    type="math",
                    value=segment.value,
                    width=width,
                    math_key=segment.math_key,
                    latex=segment.latex,
                    bbox=segment.bbox,
                )
            )
            continue
        for match in _TOKEN.finditer(segment.value):
            token = match.group(0)
            if not token:
                continue
            width = sum(_char_width(ch, size, style) for ch in token)
            atoms.append(Atom(type="text", value=token, width=width))
    return atoms


def _x_overlaps(a, b) -> bool:
    return a[0] < b[2] - 1 and b[0] < a[2] - 1


def _avoid(
    obstacles: list[tuple[float, float, float, float]],
    cursor: float,
    pitch: float,
    x0: float,
    x1: float,
    page_bottom: float,
) -> float:
    """把行起点下推到障碍物底部之下；只避让与本栏横向重叠的矩形。"""
    for _ in range(_AVOID_STEP_LIMIT):
        blocked = None
        for obstacle in obstacles:
            if not _x_overlaps(obstacle, (x0, cursor, x1, cursor + pitch)):
                continue
            if obstacle[1] < cursor + pitch and obstacle[3] > cursor:
                blocked = obstacle
                break
        if blocked is None:
            break
        cursor = blocked[3] + 1.0
    return cursor


def _place_line(
    atoms: list[Atom],
    start_x: float,
    avail: float,
    used: float,
    last_line: bool,
) -> tuple[list[PlacedAtom], float, bool]:
    """把一行 atom 摆开；非段末行两端对齐（间隙均分剩余宽度）。"""
    if not atoms:
        return [], start_x, False
    justified = (not last_line) and len(atoms) > 1 and used < avail - 0.5
    gap = (avail - used) / (len(atoms) - 1) if justified else 0.0
    placed: list[PlacedAtom] = []
    x = start_x
    for atom in atoms:
        placed.append(PlacedAtom(atom=atom, x=x, width=atom.width))
        x += atom.width + gap
    end_x = start_x + (avail if justified else used)
    return placed, end_x, justified


def layout_paragraph(
    paragraph: Paragraph,
    column_rect: tuple[float, float],
    style: LayoutStyle,
    obstacles: list[tuple[float, float, float, float]],
    page_bottom: float,
    start_y: float | None = None,
) -> tuple[list[LineBox], bool]:
    """按栏宽把一个段落排成行盒。返回 (行盒, 是否放不下)。

    ``start_y`` 由页级排版传入：段落默认锚定自己的 ``anchor.y0``（方案 A），
    但同栏前一段排完后若已越过这个位置，必须从已占用底部接着排，否则解析层
    bbox 重叠（如侧栏两条信息同 y）会让两段中文叠在同一基线上。
    """
    x0, x1 = column_rect
    if x1 - x0 < style.body_size * 2:
        return [], True
    atoms = tokenize_segments(paragraph.segments, style)
    if not atoms:
        return [], False

    pitch = style.pitch
    ascent = style.body_size * 0.8
    descent = style.body_size * 0.2
    cursor = float(paragraph.anchor[1] if start_y is None else start_y)
    lines: list[LineBox] = []
    index = 0
    first_line = True
    while index < len(atoms):
        start_x = x0 + (style.indent if first_line else 0.0)
        avail = x1 - start_x
        used = 0.0
        take: list[Atom] = []
        while index < len(atoms):
            atom = atoms[index]
            if take and used + atom.width > avail:
                break
            take.append(atom)
            used += atom.width
            index += 1
            if used > avail:  # 单个 atom 比整栏还宽：独占一行，留给 P2 缩放
                break
        if not take:
            break

        cursor = _avoid(obstacles, cursor, pitch, start_x, x1, page_bottom)
        if cursor + pitch > page_bottom:
            return lines, True

        last_line = index >= len(atoms)
        placed, end_x, justified = _place_line(take, start_x, avail, used, last_line)
        lines.append(
            LineBox(
                page=paragraph.page,
                column=paragraph.column,
                paragraph_id=paragraph.id,
                baseline=cursor + ascent,
                y0=cursor,
                y1=cursor + pitch,
                x0=start_x,
                x1=end_x,
                justified=justified,
                placed=placed,
            )
        )
        cursor += pitch
        first_line = False
    return lines, False


def layout_page_flow(
    flow: PageFlow,
    page_blocks: list[dict],
    style: LayoutStyle,
    reserved_rects: list[tuple[float, float, float, float]] | None = None,
) -> tuple[list[LineBox], list[str]]:
    """把一页的段落流排成行盒。返回 (行盒, 放不下的段落 id)。"""
    obstacles = [
        tuple(float(v) for v in block["bbox"])
        for block in page_blocks
        if block.get("kind") in OBSTACLE_KINDS and block.get("bbox")
    ]
    # 行内公式是页面级禁区：不管它属于哪个块，任何中文行都不得压上去。
    for block in page_blocks:
        for item in block.get("inline_math") or []:
            bbox = item.get("bbox")
            if bbox and len(bbox) >= 4:
                obstacles.append(tuple(float(v) for v in bbox))
    # 保留原文的块（作者单位、人名、表格、未译块）也是禁区：
    # 中文不能写到它们上面，否则就是中英叠印。
    obstacles.extend(reserved_rects or [])
    page_bottom = flow.skeleton.height - _PAGE_BOTTOM_MARGIN
    lines: list[LineBox] = []
    overflow: list[str] = []
    # 同栏纵向游标：后一段不得早于前一段排完的位置，避免锚点重叠导致同行压字。
    column_cursor: dict[int, float] = {}

    for paragraph in flow.paragraphs:
        if paragraph.kind in SKIP_KINDS:
            continue
        # 无译文的段落保留原文：不排版、不擦除，避免把英文重排一遍。
        if not paragraph.translatable:
            continue
        # P2 第一版只重排正文（正文段落 + 标题）：侧栏人名/机构与图注
        # 的版式规则尚未收口，先保留原文，避免中英叠印与图内文字混排。
        if paragraph.column == COLUMN_SIDEBAR:
            continue
        if paragraph.kind not in ("para", "abstract", "heading"):
            continue
        if paragraph.column == COLUMN_SIDEBAR:
            band = flow.skeleton.sidebar
            if band is None:
                continue
            x0, x1 = band
        elif paragraph.column == COLUMN_SPAN:
            x0, x1 = flow.skeleton.body_left, flow.skeleton.body_right
        else:
            x0, x1 = flow.skeleton.columns[paragraph.column]

        # 栏区间是 (x0, x1) 二元组；避让判断需要完整矩形，纵向先取段落起点到底。
        span_rect = (x0, paragraph.anchor[1], x1, page_bottom)
        # 只避让段落起点以下的图/表/行间公式；段落上方的障碍不影响本段。
        local = [
            obstacle for obstacle in obstacles
            if len(obstacle) >= 4
            and obstacle[1] >= paragraph.anchor[1] - 2
            and _x_overlaps(obstacle, span_rect)
        ]
        start_y = paragraph.anchor[1]
        previous = column_cursor.get(paragraph.column)
        if previous is not None and start_y < previous + 2.0:
            start_y = previous + 2.0
        paragraph_lines, full = layout_paragraph(
            paragraph, (x0, x1), style, local, page_bottom, start_y=start_y
        )
        lines.extend(paragraph_lines)
        if paragraph_lines:
            column_cursor[paragraph.column] = paragraph_lines[-1].y1
        elif start_y > column_cursor.get(paragraph.column, -1.0):
            column_cursor[paragraph.column] = start_y
        if full:
            overflow.append(paragraph.id)
    return lines, overflow


def assert_line_boxes(
    lines: list[LineBox],
    flow: PageFlow,
    obstacles: list[tuple[float, float, float, float]] | None = None,
) -> list[str]:
    """行盒硬校验：不越栏、不相交、不压障碍物。返回问题列表。"""
    problems: list[str] = []
    for line in lines:
        if line.column == COLUMN_SIDEBAR:
            if flow.skeleton.sidebar is None:
                problems.append(f"{line.paragraph_id}: 侧栏行缺少侧栏区间")
                continue
            left, right = flow.skeleton.sidebar
        elif line.column == COLUMN_SPAN:
            left, right = flow.skeleton.body_left, flow.skeleton.body_right
        else:
            left, right = flow.skeleton.columns[line.column]
        if line.x0 < left - 0.5 or line.x1 > right + 0.5:
            problems.append(f"{line.paragraph_id}: 行越出栏界")
        for obstacle in obstacles or []:
            if (
                line.x0 < obstacle[2] - 0.5
                and obstacle[0] < line.x1 - 0.5
                and line.y0 < obstacle[3] - 0.5
                and obstacle[1] < line.y1 - 0.5
            ):
                problems.append(f"{line.paragraph_id}: 行压住图/表/公式")
                break

    for index, line in enumerate(lines):
        for other in lines[index + 1:]:
            if abs(line.baseline - other.baseline) > 0.5:
                continue
            if (
                line.x0 < other.x1 - 0.5
                and other.x0 < line.x1 - 0.5
                and line.y0 < other.y1 - 0.5
                and other.y0 < line.y1 - 0.5
            ):
                problems.append(f"{line.paragraph_id}: 与 {other.paragraph_id} 同行重叠")
    return problems