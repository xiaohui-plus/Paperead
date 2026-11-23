"""译文页回填：把已译块按原 PDF 版面写回（P1 验证版）。

设计约束
- 页尺寸、页码与原 PDF 完全一致，任何块都不改变页高；
- ``equation`` / ``figure`` / ``table`` 整块保留；
- 中文断行按真实字体宽度自行计算（``insert_textbox`` 对 CJK 不认宽度）；
- 被正文块 bbox 覆盖的公式/图表，redact 后用原页高清截图贴回，观感与原版一致；
- 回填后的正文是矢量文本，可选中、可搜索。

P1 暂不处理
- 段落内部的行内公式（``inline_math`` 只有字符偏移、没有 rect），这类块默认跳过；
- 译文里的 ``$...$`` 只做统计，不渲染公式。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pymupdf

from .env import env as read_env
from .mathrender import insert_latex, latex_pdf, source_to_latex
from .inline_math import DECORATIVE_CHARS
from .textnorm import SYMBOL_FALLBACK

BODY_KINDS = frozenset(
    {
        "title",
        "author",
        "affiliation",
        "abstract",
        "heading",
        "para",
        "caption",
        "footnote",
    }
)
KEEP_KINDS = frozenset({"equation", "figure", "table"})

# 同一进程内串行化构建：并发写同一目录下的 p{n}.pdf / translated.pdf 会互相
# 占用文件，Windows 上直接 WinError 32（前端自动重建 + 用户点下载会并发）。
_BUILD_LOCK = threading.Lock()

_FONT_DIR = Path(__file__).resolve().parent / "fonts"

BODY_FONT_CANDIDATES = (
    # 朱雀仿宋：SIL OFL 1.1，可随软件分发
    _FONT_DIR / "ZhuqueFangsong-Regular.ttf",
    Path(r"C:\Windows\Fonts\simfang.ttf"),
    Path("/System/Library/Fonts/STFangsong.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
)
HEADING_FONT_CANDIDATES = (
    # 同一开源字体，标题靠更大字号 + 描边区分
    _FONT_DIR / "ZhuqueFangsong-Regular.ttf",
    Path(r"C:\Windows\Fonts\simhei.ttf"),
)

# 行首禁则：这些标点不另起一行，允许悬挂超出框宽少许。
_NO_HEAD = set("，。、；：！？）》」』】%,.;:!?)]}")

_WORD_CHARS = set("._-/")

# 误分类为正文的公式行：短、含等号/运算符，按正文回填会残留原文
# （公式字形常是 Type3/矢量，redact 擦不掉），必须在回填前识别出来。
_MATH_CHARS = set("=+−-×÷·∑√∫≈≤≥≠∞∂∇⊗⊕∈∉⊂⊆→←↔∥^_{}")



DEFAULT_LINE_HEIGHT = 1.2
DEFAULT_MIN_LINE_HEIGHT = 1.0
# 块内空白填充：默认 1.15，最多拉到 1.4，字号保持不变
MAX_FILL_LINE_HEIGHT = 1.4
BODY_FONT_SIZE = 8.6
# 自动字号：接口默认值只作报告口径，实际字号以原文正文为锚（见下）。
AUTO_BODY_SIZE = 11.5
# 中文字号锚定原文正文：仿宋方块字的字面高度约等于 em，同 pt 下比西文
# 「更满」，故按原文正文主字号的 0.90 取中文基准，避免中文明显大于英文。
CJK_SIZE_RATIO = 0.90
# 页级字号只在锚点上方 0.25pt 内浮动：跨页不允许再出现 7.6↔12.0 的跳变。
PAGE_SIZE_JITTER = 0.25
# 正文字号硬下限：低于 7.0pt 中文已不可读，宁可先压行距，再记 preserved。
BODY_HARD_MIN_SIZE = 7.0
# 极端块兜底：少数长段落在 7.0pt + 1.0 行距仍物理放不下，允许局部降到
# 6.0pt（记录在报告里），优先保证「不保留英文」，而不是整块退回原文。
BODY_FLOOR_SIZE = 6.0
# 无原文可读时的兜底候选档位（正常路径不再使用绝对档位二分）。
SIZE_STEPS = (12.0, 11.5, 10.5, 9.5, 8.6, 8.25, 7.75, 7.6)

# 页缘信息侧栏（作者单位 / 期刊信息 / 资助与利益声明）单独排版：这些列比
# 正文窄得多，沿用正文页级字号会被压得过小或直接放不下，因此有自己的
# 候选档位与下限，与正文互不影响。
SIDEBAR_SIZE_STEPS = (10.5, 9.5, 8.6, 8.25, 7.75, 7.6, 6.5)
SIDEBAR_MAX_SIZE = 10.5
SIDEBAR_MIN_SIZE = 6.5
# L1 区域层的通用几何判据（不按单篇特判）
SIDEBAR_RATIO_MIN = 0.06        # 列宽下限：过窄的是选项/公式碎片列
SIDEBAR_RATIO_MAX = 0.22        # 列宽必须小于 0.22 页宽
SIDEBAR_EDGE_RATIO = 0.32       # 列中心落在页缘 0–32% 或 68–100%
SIDEBAR_MIN_BLOCKS = 3          # 列内至少 3 行
SIDEBAR_LINE_MAX_HEIGHT = 22.0  # 单行块：块高接近正文字号
SIDEBAR_GUTTER = 6.0            # 与主正文列之间的最小横向空白沟
SIDEBAR_X1_SPREAD = 12.0        # 同一窄列各行的右缘必须对齐
SIDEBAR_Y_GAP = 40.0            # 逐行排列：相邻块纵向间距上限
SIDEBAR_BODY_MIN_RATIO = 0.34   # 页面上必须存在明显更宽的正文列
SIDEBAR_PROSE_CHARS = 120       # 成段正文否决阈值（字符数 + 句末标点）
SIDEBAR_ROW_KINDS = ("para", "author", "affiliation", "footnote", "heading")

# 译文 PDF 缓存版本：排版算法变更时递增，使旧缓存失效
PDF_ALGO_VER = "p2-autofit-v77"
# 单页渲染失败时的并发保护与日志：整篇构建不能因为某一页抛异常而失败。
log = logging.getLogger("paperead.pdf_export")


def _env_flag(name: str, default: bool = True) -> bool:
    value = read_env(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def env_layout_strict() -> bool:
    """``PAPEREAD_LAYOUT_STRICT=0``：关闭 L1/L3 的严格口径（回 v23 行为）。"""
    return _env_flag("PAPEREAD_LAYOUT_STRICT")


def env_sidebar_translate() -> bool:
    """``PAPEREAD_SIDEBAR_TRANSLATE=0``：侧栏保留原文（不翻译、不擦除）。"""
    return _env_flag("PAPEREAD_SIDEBAR_TRANSLATE") and env_layout_strict()


def env_table_translate() -> bool:
    """``PAPEREAD_TABLE_TRANSLATE=0``：表格整块保留原文（P1 起生效）。"""
    return _env_flag("PAPEREAD_TABLE_TRANSLATE") and env_layout_strict()
# 标题按正文比例统一：同一 level 全篇一致，不逐块缩。
TITLE_RATIO = 1.55
HEADING_RATIOS = {1: 1.35, 2: 1.22, 3: 1.12}
DEFAULT_MIN_SIZE = 7.6
DEFAULT_MAX_SIZE = 11.5
PAGE_BOTTOM_MARGIN = 40.0


@dataclass
class BlockLayout:
    block_id: str
    kind: str
    source_size: float
    font_size: float
    lines: int
    needed: float
    available: float
    overflow: bool = False
    cut_lines: int = 0
    note: str = ""
    line_height: float = DEFAULT_LINE_HEIGHT
    role: str = "body"


@dataclass
class PageRenderResult:
    page: int
    dry_run: bool
    layouts: list[BlockLayout] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    images_before: int = 0
    images_after: int = 0
    drawings_before: int = 0
    drawings_after: int = 0
    fonts: list[tuple] = field(default_factory=list)
    original_left: bool = False
    residue_blocks: list[str] = field(default_factory=list)
    page_size: tuple[float, float] = (0.0, 0.0)
    restored_keep_blocks: int = 0
    sidebar_bands: list[tuple[float, float]] = field(default_factory=list)
    sidebar_blocks: int = 0
    cross_leaks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "dry_run": self.dry_run,
            "page_size": list(self.page_size),
            "layouts": [vars(x) for x in self.layouts],
            "skipped": self.skipped,
            "images": [self.images_before, self.images_after],
            "drawings": [self.drawings_before, self.drawings_after],
            "fonts": [list(x) for x in self.fonts],
            "original_left": self.original_left,
            "residue_blocks": self.residue_blocks,
            "restored_keep_blocks": self.restored_keep_blocks,
            "sidebar_bands": [list(x) for x in self.sidebar_bands],
            "sidebar_blocks": self.sidebar_blocks,
            "cross_leaks": self.cross_leaks,
        }


def pick_font(candidates: Iterable[Path] = BODY_FONT_CANDIDATES) -> Path:
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("未找到可用的中文字体")


def pick_body_font() -> Path:
    return pick_font(BODY_FONT_CANDIDATES)


def pick_heading_font() -> Path:
    return pick_font(HEADING_FONT_CANDIDATES)


def load_font(font_path: Path | str) -> pymupdf.Font:
    return pymupdf.Font(fontfile=str(font_path))


def _rect(block: dict, page_index: int) -> pymupdf.Rect | None:
    """取块在本页的矩形。

    ``Block.page`` / ``bboxes`` 的页码是 **1-indexed**（与 PDF 页码一致），
    而 ``page_index`` 是 PyMuPDF 的 0-indexed。两者混用会整体错一页：第 5 页
    的译文被写到第 6 页，表格因此不会被当成禁排区。
    """
    page_no = page_index + 1
    for item in block.get("bboxes") or []:
        if int(item[0]) == page_no:
            return pymupdf.Rect(float(item[1]), float(item[2]), float(item[3]), float(item[4]))
    if int(block.get("page", -1)) == page_no:
        x0, y0, x1, y1 = (float(v) for v in block.get("bbox") or (0, 0, 0, 0))
        return pymupdf.Rect(x0, y0, x1, y1)
    return None


def _overlaps_x(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
    return a.x0 < b.x1 - 2 and b.x0 < a.x1 - 2


def next_limit(
    rect: pymupdf.Rect,
    page_blocks: list[tuple[dict, pymupdf.Rect]],
    page_height: float,
) -> float:
    """当前块下方可用的最低边界：横向重叠的下一块上沿，或页底保留区。"""
    limit = page_height - PAGE_BOTTOM_MARGIN
    for _block, other in page_blocks:
        if other == rect:
            continue
        if not _overlaps_x(rect, other):
            continue
        if other.y0 >= rect.y1 - 2:
            limit = min(limit, other.y0 - 2)
    return max(limit, rect.y1)


def _source_size(page: pymupdf.Page, rect: pymupdf.Rect, fallback: float) -> float:
    """从原页 span 读取该块的主字号，避免依赖 blocks.json 未序列化的 size。"""
    sizes: list[float] = []
    try:
        data = page.get_text("dict", clip=rect)
    except Exception:
        return fallback
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                cx = (span["bbox"][0] + span["bbox"][2]) / 2
                cy = (span["bbox"][1] + span["bbox"][3]) / 2
                if rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1:
                    sizes.append(float(span.get("size") or 0))
    sizes = [s for s in sizes if s > 0]
    if not sizes:
        return fallback
    sizes.sort()
    return sizes[len(sizes) // 2]


def _page_body_source_size(page: pymupdf.Page) -> float:
    """该页原文正文的主字号（按字符数加权的众数）。

    只作字号锚点，不做语义判断：正文的字符数远多于页眉、脚注与图内标注，
    加权众数就是正文常用字号。取不到时回退 ``BODY_FONT_SIZE``。
    """
    counts: dict[float, int] = {}
    try:
        data = page.get_text("dict")
    except Exception:
        return BODY_FONT_SIZE
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = (span.get("text") or "").strip()
                size = float(span.get("size") or 0)
                if not text or size <= 0:
                    continue
                key = round(size, 2)
                counts[key] = counts.get(key, 0) + len(text)
    if not counts:
        return BODY_FONT_SIZE
    return max(counts.items(), key=lambda item: item[1])[0]


def _body_size_candidates(anchor: float) -> list[float]:
    """围绕「原文正文 × CJK_SIZE_RATIO」生成从高到低的页级字号档。

    字号不再取自固定的 12…7.6 绝对档位，而是跟着原文正文走；页间差异
    因此被限制在 ±0.25pt 与「原文本身的小字号差异」两层之内。
    """
    base = max(BODY_HARD_MIN_SIZE, anchor * CJK_SIZE_RATIO)
    out: list[float] = []
    for index in range(9):
        value = round(base + PAGE_SIZE_JITTER - index * 0.25, 2)
        if value < BODY_HARD_MIN_SIZE - 0.001 or value in out:
            continue
        out.append(value)
    return out or [BODY_HARD_MIN_SIZE]


def wrap_text(font: pymupdf.Font, text: str, width: float, size: float) -> list[str]:
    """按真实字形宽度断行，中文任意位置可断，英文保持整词。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        raw = raw.strip()
        if not raw:
            if lines and lines[-1]:
                lines.append("")
            continue
        cur = ""
        i = 0
        n = len(raw)
        while i < n:
            ch = raw[i]
            if ch.isascii() and (ch.isalnum() or ch in _WORD_CHARS):
                j = i
                while j < n and raw[j].isascii() and (raw[j].isalnum() or raw[j] in _WORD_CHARS):
                    j += 1
                token = raw[i:j]
                if cur and font.text_length(cur + token, size) > width:
                    lines.append(cur)
                    cur = token
                else:
                    cur += token
                i = j
                continue
            if font.text_length(cur + ch, size) <= width:
                cur += ch
            elif ch in _NO_HEAD and cur:
                cur += ch
                lines.append(cur)
                cur = ""
            else:
                lines.append(cur)
                cur = ch
            i += 1
        if cur:
            lines.append(cur)
    return [line for line in lines if line != ""] or [""]


def layout_block(
    font: pymupdf.Font,
    text: str,
    rect: pymupdf.Rect,
    source_size: float,
    *,
    min_size: float = DEFAULT_MIN_SIZE,
    max_size: float = DEFAULT_MAX_SIZE,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> tuple[float, list[str], float]:
    """返回 (最终字号, 行列表, 需要高度)。优先保原文高度，其次缩字号。"""
    size = min(max(source_size or DEFAULT_MAX_SIZE, min_size), max_size)
    width = max(rect.width - 1.0, 8.0)
    height = max(rect.height, 1.0)

    chosen = min_size
    chosen_lines = wrap_text(font, text, width, min_size)
    while size >= min_size:
        lines = wrap_text(font, text, width, size)
        needed = (font.ascender - font.descender) * size + max(0, len(lines) - 1) * size * line_height
        if needed <= height + 0.5:
            return size, lines, needed
        chosen, chosen_lines = size, lines
        size -= 0.25

    lines = wrap_text(font, text, width, min_size)
    needed = (font.ascender - font.descender) * min_size + max(0, len(lines) - 1) * min_size * line_height
    return min_size, lines, needed


def _inflate(rect: pymupdf.Rect, d: float = 0.8) -> pymupdf.Rect:
    """横带/禁排区外扩一点：擦净文字边缘，同时给公式留安全边。"""
    return pymupdf.Rect(rect.x0 - d, rect.y0 - d, rect.x1 + d, rect.y1 + d)


def _redact_outside(rect: pymupdf.Rect, obstacles: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """把矩形按禁排区的 x 区间切分，只返回公式之外的碎片。"""
    pieces = [pymupdf.Rect(rect)]
    for obs in obstacles:
        nxt: list[pymupdf.Rect] = []
        for piece in pieces:
            inter = piece & obs
            if inter.is_empty:
                nxt.append(piece)
                continue
            if inter.x0 - piece.x0 > 0.5:
                nxt.append(pymupdf.Rect(piece.x0, piece.y0, inter.x0, piece.y1))
            if piece.x1 - inter.x1 > 0.5:
                nxt.append(pymupdf.Rect(inter.x1, piece.y0, piece.x1, piece.y1))
        pieces = nxt
    return [
        p for p in pieces
        if not p.is_empty and p.width > 0.3 and p.height > 0.3
    ]


def _redact_bands_avoid_math(
    rect: pymupdf.Rect, obstacles: list[pymupdf.Rect]
) -> list[pymupdf.Rect]:
    """把横带减去公式矩形（上下左右二维挖洞），而不是只按一个方向切。

    只按 y 切会让公式所在的**整行宽度**都不擦——公式 bbox 通常只占行中一段，
    同行两侧全是正文英文，结果每页在公式行上残留一列英文（用户报「很有规律、
    一列一列的原文」）。只按 x 切则相反：公式多为整行宽，横带被整条判成重叠
    而返回空，正文一点都不擦。正确做法是把公式自身那个矩形挖掉，其余碎片
    （同行左右、上下相邻行）全部照擦。公式本体（Type3/矢量字形）保留原样。
    """
    pieces = [pymupdf.Rect(rect)]
    for obs in obstacles:
        nxt: list[pymupdf.Rect] = []
        for piece in pieces:
            inter = piece & obs
            if inter.is_empty or inter.width <= 0.5 or inter.height <= 0.5:
                nxt.append(piece)
                continue
            if inter.y0 - piece.y0 > 0.5:
                nxt.append(pymupdf.Rect(piece.x0, piece.y0, piece.x1, inter.y0))
            if piece.y1 - inter.y1 > 0.5:
                nxt.append(pymupdf.Rect(piece.x0, inter.y1, piece.x1, piece.y1))
            if inter.x0 - piece.x0 > 0.5:
                nxt.append(pymupdf.Rect(piece.x0, inter.y0, inter.x0, inter.y1))
            if piece.x1 - inter.x1 > 0.5:
                nxt.append(pymupdf.Rect(inter.x1, inter.y0, piece.x1, inter.y1))
        pieces = nxt
    return [
        p for p in pieces
        if not p.is_empty and p.width > 0.3 and p.height > 0.3
    ]


def plan_bands(
    rect: pymupdf.Rect,
    obstacles: list[pymupdf.Rect],
    min_width_ratio: float = 0.45,
    min_height: float = 2.0,
) -> list[pymupdf.Rect]:
    """把段落区域切成可排字的横带，绕开公式/图表等禁排区。

    排字时用默认阈值丢弃窄带；擦除时传 ``min_width_ratio=0`` 把窄带也擦掉，
    否则公式旁边的窄条会留下英文碎片。
    """
    if not obstacles:
        return [pymupdf.Rect(rect)]
    ys = {rect.y0, rect.y1}
    for obs in obstacles:
        inter = pymupdf.Rect(rect) & obs
        if inter.is_empty:
            continue
        ys.add(max(inter.y0, rect.y0))
        ys.add(min(inter.y1, rect.y1))
    cuts = sorted(v for v in ys if rect.y0 <= v <= rect.y1)
    bands: list[pymupdf.Rect] = []
    for y0, y1 in zip(cuts, cuts[1:]):
        if y1 - y0 < min_height:
            continue
        blockers: list[tuple[float, float]] = []
        for obs in obstacles:
            inter = pymupdf.Rect(rect) & obs
            if inter.is_empty:
                continue
            if inter.y0 < y1 and inter.y1 > y0:
                blockers.append((max(inter.x0, rect.x0), min(inter.x1, rect.x1)))
        segments: list[tuple[float, float]] = []
        cursor = rect.x0
        for bx0, bx1 in sorted(blockers):
            if bx0 > cursor:
                segments.append((cursor, bx0))
            cursor = max(cursor, bx1)
        if cursor < rect.x1:
            segments.append((cursor, rect.x1))
        if not segments:
            continue
        x0, x1 = max(segments, key=lambda s: s[1] - s[0])
        if x1 - x0 < rect.width * min_width_ratio:
            continue
        bands.append(pymupdf.Rect(x0, y0, x1, y1))

    merged: list[pymupdf.Rect] = []
    for band in bands:
        if (
            merged
            and abs(band.x0 - merged[-1].x0) < 0.5
            and abs(band.x1 - merged[-1].x1) < 0.5
            and abs(band.y0 - merged[-1].y1) < 0.5
        ):
            merged[-1] = pymupdf.Rect(band.x0, merged[-1].y0, band.x1, band.y1)
        else:
            merged.append(band)
    return merged


def wrap_prefix(
    font: pymupdf.Font,
    text: str,
    width: float,
    size: float,
    max_lines: int,
) -> tuple[list[str], str]:
    """在给定宽度内排前 ``max_lines`` 行，返回 (行列表, 未排完的剩余文本)。"""
    if max_lines <= 0 or not text:
        return [], text
    lines: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n and len(lines) < max_lines:
        ch = text[i]
        if ch == "\n":
            lines.append(text[start:i].strip())
            i += 1
            start = i
            continue
        if i == start and ch == " ":
            i += 1
            start = i
            continue
        if font.text_length(text[start : i + 1], size) <= width:
            i += 1
            continue
        cut = i
        if text[i].isascii() and (text[i].isalnum() or text[i] in _WORD_CHARS):
            token_start = i
            while (
                token_start > start
                and text[token_start - 1].isascii()
                and (text[token_start - 1].isalnum() or text[token_start - 1] in _WORD_CHARS)
            ):
                token_start -= 1
            # 英文单词整词换行，避免 Transformer 被拆成 Transforme / r
            if (
                token_start > start
                and font.text_length(text[token_start : i + 1], size) <= width
            ):
                cut = token_start
        elif ch in _NO_HEAD and start < i:
            cut = i + 1
        lines.append(text[start:cut].strip())
        start = cut
        while start < n and text[start] == " ":
            start += 1
        i = start
    if start < n and len(lines) < max_lines:
        lines.append(text[start:].strip())
        start = n
    return [line for line in lines if line], text[start:]


def flow_in_bands(
    font: pymupdf.Font,
    text: str,
    bands: list[pymupdf.Rect],
    size: float,
    line_height: float,
) -> tuple[list[tuple[pymupdf.Rect, list[str]]], str]:
    """把译文按横带顺序流式排入，返回 (每带的行, 剩余文本)。"""
    placements: list[tuple[pymupdf.Rect, list[str]]] = []
    remaining = text
    for band in bands:
        capacity = int((band.height + 0.5) // (size * line_height))
        if capacity <= 0 or not remaining.strip():
            continue
        lines, remaining = wrap_prefix(font, remaining, max(band.width - 1, 8), size, capacity)
        if lines:
            placements.append((band, lines))
    return placements, remaining


def layout_in_bands(
    font: pymupdf.Font,
    text: str,
    bands: list[pymupdf.Rect],
    source_size: float,
    *,
    min_size: float = DEFAULT_MIN_SIZE,
    max_size: float = DEFAULT_MAX_SIZE,
    line_height: float = DEFAULT_LINE_HEIGHT,
    min_line_height: float = DEFAULT_MIN_LINE_HEIGHT,
) -> tuple[float, float, list[tuple[pymupdf.Rect, list[str]]], str]:
    """字号优先：先逐级压行距到下限，仍放不下才降字号。

    返回 (字号, 行距, 每带的行, 剩余文本)。这样长段落不会把整篇字号拖小，
    只在该段行距略紧。
    """
    size = min(max(source_size or DEFAULT_MAX_SIZE, min_size), max_size)
    lh = line_height
    while size >= min_size:
        placements, remaining = flow_in_bands(font, text, bands, size, lh)
        if not remaining.strip():
            return size, lh, placements, remaining
        if lh > min_line_height:
            lh = max(min_line_height, round(lh - 0.04, 3))
            continue
        lh = line_height
        size -= 0.25
    placements, remaining = flow_in_bands(
        font, text, bands, min_size, min_line_height
    )
    return min_size, min_line_height, placements, remaining


HARD_MIN_SIZE = 5.0  # 仅图内短标注的局部下限；正文用 BODY_HARD_MIN_SIZE=7.0
# 差一两行的块继续缩小并报告标出，不再因此整块保留英文。


def plan_row_bands(
    rect: pymupdf.Rect,
    obstacles: list[pymupdf.Rect],
    size: float,
    line_height: float,
) -> list[tuple[float, float, float, float]]:
    """逐行计算可用区间：图只占右上角时，上半段窄、图底以下恢复整栏宽。

    这是「行级可用宽度」的唯一来源：不再把障碍延伸成整段高度，也不因为
    上方有图就把整段压成窄条。
    """
    step = max(size * line_height, 1.0)
    rows: list[tuple[float, float, float, float]] = []
    y = rect.y0
    while y + step * 0.45 <= rect.y1:
        y0, y1 = y, min(y + step, rect.y1)
        covering = []
        for obs in obstacles:
            inter = pymupdf.Rect(rect) & obs
            if inter.is_empty:
                continue
            if inter.y0 < y1 and inter.y1 > y0:
                covering.append(inter)
        x0, x1 = rect.x0, rect.x1
        for inter in covering:
            # 公式与图表一样不可写：行与禁排区纵向相交超过 30% 时整行跳过，
            # 中文只在公式之外的行流动；放不下由字号适配，绝不压公式。
            overlap_h = min(y1, inter.y1) - max(y0, inter.y0)
            if overlap_h > (y1 - y0) * 0.3:
                x0 = x1
                break
            if inter.x0 <= x0 + 1 and inter.x1 >= x1 - 1:
                x0 = x1
                break
            # 障碍靠左：可用区从障碍右沿开始；障碍靠右：到障碍左沿结束。
            if (inter.x0 + inter.x1) / 2 <= rect.x0 + rect.width * 0.5:
                x0 = max(x0, inter.x1)
            else:
                x1 = min(x1, inter.x0)
        if x1 - x0 >= 20:
            rows.append((x0, y0, x1, y1))
        y += step
    return rows


def flow_in_rows(
    font: pymupdf.Font,
    text: str,
    rows: list[tuple[float, float, float, float]],
    size: float,
) -> tuple[list[tuple[pymupdf.Rect, list[str]]], str]:
    """按逐行可用宽度顺序装字：每行用自己的宽度断行，装满进入下一行。"""
    if not rows:
        return [], text
    runs: list[list[tuple[float, float, float, float]]] = []
    for row in rows:
        if (
            runs
            and abs(row[0] - runs[-1][0][0]) < 0.5
            and abs(row[2] - runs[-1][0][2]) < 0.5
            and abs(row[1] - runs[-1][-1][3]) < 0.5
        ):
            runs[-1].append(row)
        else:
            runs.append([row])
    placements: list[tuple[pymupdf.Rect, list[str]]] = []
    remaining = text
    for run in runs:
        if not remaining.strip():
            break
        x0, y0, x1, _ = run[0]
        capacity = len(run)
        lines, remaining = wrap_prefix(
            font, remaining, max(x1 - x0 - 1, 8), size, capacity
        )
        if lines:
            placements.append(
                (pymupdf.Rect(x0, y0, x1, run[-1][3]), lines)
            )
    return placements, remaining


def layout_in_rows(
    font: pymupdf.Font,
    text: str,
    rect: pymupdf.Rect,
    obstacles: list[pymupdf.Rect],
    source_size: float,
    *,
    min_size: float = BODY_HARD_MIN_SIZE,
    max_size: float = DEFAULT_MAX_SIZE,
    line_height: float = DEFAULT_LINE_HEIGHT,
    min_line_height: float = DEFAULT_MIN_LINE_HEIGHT,
) -> tuple[float, float, list[tuple[pymupdf.Rect, list[str]]], str]:
    """先压行距、再降字号；每降一档字号都从默认行距重新开始。

    旧实现先把字号一路降到 5.0pt 才动行距，导致同一页正文出现 7.6/6.97/
    4.98 多种字号。现在字号是页级契约，块内只允许在 1.2→1.0 之间压行距；
    压到底仍放不下才降字号，且最低 7.0pt（再低中文不可读）。
    """
    size = min(max(source_size or DEFAULT_MAX_SIZE, min_size), max_size)
    lh = line_height
    placements: list[tuple[pymupdf.Rect, list[str]]] = []
    remaining = text
    while True:
        rows = plan_row_bands(rect, obstacles, size, lh)
        placements, remaining = flow_in_rows(font, text, rows, size)
        if not remaining.strip():
            return size, lh, placements, remaining
        if lh > min_line_height + 0.001:
            lh = max(min_line_height, round(lh - 0.05, 3))
            continue
        if size <= min_size + 0.001:
            break
        size = round(max(min_size, size - 0.25), 2)
        lh = line_height
    return size, min_line_height, placements, remaining


def _contains_inner_math(
    rect: pymupdf.Rect,
    keep_blocks: list[tuple[dict, pymupdf.Rect]],
) -> bool:
    for block, other in keep_blocks:
        if block.get("kind") != "equation":
            continue
        inter = pymupdf.Rect(rect) & other
        if not inter.is_empty and inter.get_area() > 4:
            return True
    return False


def _inline_math_ok(block: dict, size: float) -> bool:
    """块内公式是否适合内联进中文句子。

    解析层常把一个公式拆成「ε / × / σ」这类单字符碎片，逐 token 内联会让
    符号散落在段落各处。只有公式少（≤4）且每个都是完整表达式时才内联；
    其余交给禁区绕排，公式原样保留在原位。
    """
    items = block.get("inline_math") or []
    if not items or len(items) > 4:
        return False
    for item in items:
        bbox = item.get("bbox")
        if not bbox or len(bbox) < 4:
            return False
        if (bbox[2] - bbox[0]) < size * 1.5 and (bbox[3] - bbox[1]) < size * 1.2:
            return False
    return True


def _is_margin_block(block: dict, page_height: float) -> bool:
    """页眉/页脚：贴近页缘、行高不超过两行，不翻译也不擦除。

    这类内容（期刊名、卷期、作者名、页码）翻译或回填都会破坏原版式。
    """
    bbox = block.get("bbox")
    if not bbox or len(bbox) < 4 or page_height <= 0:
        return False
    if (bbox[3] - bbox[1]) > 30.0:
        return False
    center_y = (bbox[1] + bbox[3]) / 2
    return center_y < page_height * 0.06 or center_y > page_height * 0.94


def _merge_graphic_rects(
    rects: list[pymupdf.Rect], gap: float = 6.0
) -> list[pymupdf.Rect]:
    """把相距很近的图形矩形并成图区，避免同一张图被拆成几十个互不相交的小簇。"""
    pending = [pymupdf.Rect(r) for r in rects if not r.is_empty]
    merged: list[pymupdf.Rect] = []
    while pending:
        current = pending.pop()
        changed = True
        while changed:
            changed = False
            rest: list[pymupdf.Rect] = []
            for other in pending:
                probe = pymupdf.Rect(
                    other.x0 - gap, other.y0 - gap, other.x1 + gap, other.y1 + gap
                )
                if not (current & probe).is_empty:
                    current |= other
                    changed = True
                else:
                    rest.append(other)
            pending = rest
        merged.append(current)
    return merged


def page_graphic_regions(
    page: pymupdf.Page, page_blocks: list[tuple[dict, pymupdf.Rect]]
) -> list[pymupdf.Rect]:
    """页面上的图形区域：位图、矢量簇、figure 块。

    系统图 / 流程图在 PDF 里往往没有位图，只有几百条矢量线段（Nature 的
    Figure 1 就是 324 条）。这类区域里的文字是图内标注，不是正文段落，
    排版层必须知道它们的边界，否则会把整页图当正文重排。
    """
    page_area = max(page.rect.width * page.rect.height, 1.0)
    rects = [
        pymupdf.Rect(b["bbox"])
        for b, _rect in page_blocks
        if b.get("kind") == "figure" and b.get("bbox")
    ]
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox:
                rect = pymupdf.Rect(bbox)
                if rect.get_area() >= page_area * 0.03:
                    rects.append(rect)
    except Exception:
        pass
    try:
        for rect in page.cluster_drawings():
            rect = pymupdf.Rect(rect)
            if rect.get_area() >= page_area * 0.01:
                rects.append(rect)
    except Exception:
        pass
    return [
        rect for rect in _merge_graphic_rects(rects) if rect.get_area() >= page_area * 0.02
    ]


def _inside_graphic_region(rect: pymupdf.Rect, regions: list[pymupdf.Rect]) -> bool:
    """块是否属于图内文字：与某个图区的重叠占自身一半以上。"""
    if rect.is_empty:
        return False
    area = rect.get_area()
    for region in regions:
        probe = pymupdf.Rect(
            region.x0 - 6, region.y0 - 6, region.x1 + 6, region.y1 + 6
        )
        inter = rect & probe
        if not inter.is_empty and inter.get_area() >= area * 0.5:
            return True
    return False


def page_is_graphic_page(
    page: pymupdf.Page,
    page_blocks: list[tuple[dict, pymupdf.Rect]],
    body_size: float,
) -> bool:
    """整页是否是一张大图（矢量系统图 / 整版位图）。

    图区聚类只能覆盖「有图形元素」的地方，图内文字之间往往没有线条，会
    落在聚类空隙里（Nature Figure 1 的 c 区就如此）。整页图还需要一条
    页面级判据：大量矢量绘制 / 整幅位图 **且** 页面主字号明显小于正文。
    加上字号条件是为了避免把「小字号正文页」误判成图页而整页不译。
    """
    page_area = max(page.rect.width * page.rect.height, 1.0)
    try:
        drawings = len(page.get_drawings())
    except Exception:
        drawings = 0
    has_big_image = False
    try:
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox and pymupdf.Rect(bbox).get_area() >= page_area * 0.4:
                has_big_image = True
                break
    except Exception:
        pass
    if drawings < 150 and not has_big_image:
        return False
    sizes: list[float] = []
    try:
        data = page.get_text("dict")
    except Exception:
        return False
    for raw_block in data.get("blocks", []):
        for line in raw_block.get("lines", []):
            for span in line.get("spans", []):
                size = float(span.get("size") or 0)
                if size > 0:
                    sizes.append(size)
    if not sizes or body_size <= 0:
        return False
    sizes.sort()
    median = sizes[len(sizes) // 2]
    return median < body_size * 0.85


_SENTENCE_TAIL = re.compile(r"[.!?。！？；;]\s*$")


def _looks_like_figure_label(text: str) -> bool:
    """图内短标签（"AI"、"Worker"、"Research plan"）不翻译。

    这类词翻译后没有信息增量，却会在窄框里折行、压到图形上；真正需要
    翻译的是图内成句说明（"Scientist describes a research goal…"）。
    """
    value = " ".join((text or "").split())
    if not value:
        return False
    if len(value) > 32:
        return False
    if _SENTENCE_TAIL.search(value):
        return False
    return len(value.split()) <= 4


def _is_page_footnote(
    page: pymupdf.Page,
    block: dict,
    rect: pymupdf.Rect,
    page_height: float,
    body_size: float,
) -> bool:
    """页底脚注 / 作者单位：位置贴近页底、字号小于正文，不翻译也不擦。

    期刊首页底部常把作者单位、通讯邮箱排成一整段，解析会把它标成 ``para``。
    当正文翻译会让这段变成一大块中文并压住页码；这里用「位置 + 源字号」
    两条通用判据识别，不按单篇特判。
    """
    if page_height <= 0 or rect.height < 8 or rect.height > 90:
        return False
    if rect.y0 < page_height * 0.86:
        return False
    src = _source_size(page, rect, body_size)
    return bool(src) and bool(body_size) and src < body_size * 0.95


def _figure_text_has_gaps(
    page: pymupdf.Page, rect: pymupdf.Rect, min_gap: float = 18.0
) -> bool:
    """块内是否并排了多个独立标签 / 覆盖了多个框。

    解析会把同一行的 "Generation agent / Proximity agent / Worker" 并成
    一块，甚至把左框说明 + 中间框标题 + 右框说明并成一块。它们的共同 bbox
    横跨多个框，译文写进去必然错位；整块保留原文（不擦不写）。
    """
    try:
        words = page.get_text("words", clip=rect)
    except Exception:
        return False
    if len(words) < 2:
        return False
    centers = sorted((float(word[0]) + float(word[2])) / 2.0 for word in words)
    # 词心之间出现大空白：说明这不是一个连续文本块，而是多个并排元素被
    # 并进了同一 bbox。阈值 18pt 远大于正常词距（3–6pt），不会误伤句子。
    for index in range(1, len(centers)):
        if centers[index] - centers[index - 1] > min_gap:
            return True
    return False


def _figure_text_size(
    page: pymupdf.Page,
    block: dict,
    rect: pymupdf.Rect,
    figure_ids: set[int],
    body_size: float,
) -> float:
    """图内文字的起排字号：不超过源标注字号的 1.25 倍。

    图内标签常见 6–7pt，直接用正文 9.5pt 起排会立刻溢出、再被压到 5pt；
    以源字号为基准既保留「与图协调」的观感，也能让中文在窄框里放下。
    """
    if id(block) not in figure_ids or body_size <= 0:
        return body_size
    src = _source_size(page, rect, body_size)
    if not src:
        return body_size
    return min(body_size, max(HARD_MIN_SIZE, src * 1.25))


def is_same_text(block: dict) -> bool:
    """译文与原文相同（URL、编号、纯符号），回填没有意义，直接保留原文。"""
    src = " ".join((block.get("text") or "").split())
    dst = " ".join((block.get("translated") or "").split())
    return bool(src) and src == dst


_FORMULA_SRC_RE = re.compile(r"[=≤≥≈≠<>+\-*/^_∑∏∫√⊗⊕∈∪∩·×∂∇]")


def _looks_like_formula_source(text: str) -> bool:
    """原文本身是否就是一行公式（决定「回抄译文」能否按公式保留）。"""
    value = (text or "").strip()
    if not value or len(value) > 140:
        return False
    if not _FORMULA_SRC_RE.search(value):
        return False
    # 句末带标点且较长，多半是正文句子而不是公式行
    if len(value) > 40 and value[-1:] in ".,;:?!":
        return False
    if len(re.findall(r"[A-Za-z]{3,}", value)) >= 8:
        return False
    return True


def is_formula_echo(block: dict) -> bool:
    """模型把公式行原样回抄（译文几乎没有中文），应按公式行保留原文。

    `b0056` 的 translated 是 `Attnk(hi, hj) = softmax(aij) · vj`，没有翻译；
    若按正文回填，公式裁图会被拆成两段、文本只剩 `Attn ) = softmax(`。

    只有当原文确实是公式行时才允许跳过；否则模型偶发输出英文（翻译失败）
    会让整段正文被当成公式保留、留下整段英文。
    """
    src = (block.get("text") or "").strip()
    dst = (block.get("translated") or "").strip()
    if not src or not dst:
        return False
    if (block.get("kind") or "") == "equation":
        return True
    if not _looks_like_formula_source(src):
        return False
    cjk = sum(1 for ch in dst if "\u4e00" <= ch <= "\u9fff")
    if cjk / max(len(dst), 1) < 0.05:
        return True
    if len(dst) < 60:
        import difflib

        norm = lambda value: re.sub(r"\s+", "", value).lower()  # noqa: E731
        if difflib.SequenceMatcher(None, norm(src), norm(dst)).ratio() >= 0.9:
            return True
    return False


def is_math_like(block: dict) -> bool:
    """短、含运算符且符号密度高的 para，多半是被误分类的公式行。"""
    if block.get("kind") != "para":
        return False
    # 有完整中文译文的块必须回填；含少量公式符号不能当公式行跳过，
    # 否则 `where q_i = ...` 这类公式解释行会整段保留英文。
    if len((block.get("translated") or "").strip()) >= 10:
        return False
    text = (block.get("text") or "").strip()
    if not text or len(text) > 90:
        return False
    if not any(ch in text for ch in "=·√⊗∑"):
        return False
    hits = sum(1 for ch in text if ch in _MATH_CHARS)
    return hits >= 2 and hits / len(text) >= 0.02



# 旧译文里常残留英文连接词/引导语（Where/Here/The symbol ... represents），
# 与中文混排会明显影响阅读；只清句首，避免误删正文里的英文术语。
_LEAD_EN = re.compile(
    r"^(?:where|here|the symbol\s+\S+\s+represents|and|but)\s+", re.I
)

# 朱雀仿宋缺这些数学/图标字形，直接写会变成空白或方块。
_BAD_GLYPH_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ue000-\uf8ff\ufffd\u25a1\u25a0\u25ae\u25af]")
_MATH_ALNUM_RE = re.compile(r"[\U0001D400-\U0001D7FF]")


def _renderable_text(text: str) -> str:
    """公式兜底文本必须能渲染：清掉私用区/方框，数学字母归一成 ASCII。"""
    value = _BAD_GLYPH_RE.sub(" ", str(text or ""))
    value = _MATH_ALNUM_RE.sub(
        lambda m: unicodedata.normalize("NFKC", m.group(0)), value
    )
    return re.sub(r"\s+", " ", value).strip()


def _font_safe_text(text: str, font: pymupdf.Font) -> str:
    """把文本压成「该字体一定能渲染」的形式，杜绝 glyph 0 黑块。

    三层：清控制符/私用区/方块 → 缺字形符号按替换表换等价写法 →
    逐字检查 ``font.has_glyph``，仍没有字形的字符直接丢弃（宁可少一个
    符号，也不能在页面上留下 \x00 黑块）。
    """
    value = _renderable_text(text)
    if not value:
        return ""
    changed = []
    for ch in value:
        if ch.isspace() or font.has_glyph(ord(ch)):
            changed.append(ch)
            continue
        rep = _SYMBOL_FALLBACK.get(ch)
        if rep:
            changed.append(rep)
    return "".join(changed)


# 字体缺字形替换表（与排版层共用，见 ``textnorm.SYMBOL_FALLBACK``）。
# 朱雀仿宋没有集合/双线体/逻辑符号的字形，直接写进 PDF 会得到 glyph 0
# （文本层是 \x00\x00，视觉上是黑块）。凡是「写入中文页」的文本都必须
# 先过这张表，再按字体逐字兜底。
_SYMBOL_FALLBACK = SYMBOL_FALLBACK


def strip_block_leading_title(text: str, source: str) -> str:
    """短块的译文开头误抄了相邻标题时，只保留真正的译文段落。

    典型：原文 `Within the Experimental Network`（31 字符、单行）的译文是
    `实验结果\n\n实验网络内`，把上一节标题也串进来了，导致两行放不下、
    整块被迫保留英文。只对「原文短且单行」的块生效，长段落不动。
    """
    src = (source or "").strip()
    if not text or "\n" not in text or not src or "\n" in src or len(src) > 60:
        return text
    parts = [part.strip() for part in text.split("\n") if part.strip()]
    if len(parts) < 2:
        return text
    # 首段是短标签（标题/小标题），末段才是本块译文。
    if len(parts[0]) <= 20 and len(parts[-1]) >= max(2, len(parts[0]) // 2):
        return parts[-1]
    return text


def clean_translation(text: str, inline_math: list[dict] | None) -> str:
    """清掉译文里被压平的公式纯文本，并替换字体缺失的符号。

    行内公式原样保留在页面上（不擦），译文里由模型抄回来的 ``PquerySPD``
    这类纯文本必须删掉，否则会和原公式重复。仿宋/黑体没有 ``≪`` ``≫``
    字形，不替换会在 PDF 里变成空白。
    """
    out = text
    for item in inline_math or []:
        source = (item.get("source") or "").strip()
        if not source:
            continue
        chars = [ch for ch in source if not ch.isspace()]
        if not chars:
            continue
        pattern = re.compile(r"\s*".join(re.escape(ch) for ch in chars))
        out = pattern.sub("", out)

    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    # 私用区/替换符/方框占位在回填字体里同样没有字形，写进 PDF 就是「□」。
    # 行内公式应已由裁图渲染，这里剩下的只是无信息的占位符，直接删掉。
    out = re.sub(r"[\ue000-\uf8ff\ufffd\u25a1]", "", out)
    for ch, rep in _SYMBOL_FALLBACK.items():
        if ch in out:
            out = out.replace(ch, rep)
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    for _ in range(2):
        trimmed = _LEAD_EN.sub("", out)
        if trimmed == out:
            break
        out = trimmed
    return out.strip()


_LOOSE_PUNCT = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "、": ",",
        "！": "!",
        "？": "?",
        "　": " ",
    }
)


def _loose_map(text: str) -> tuple[str, list[int]]:
    """去空白 + 标点归一后的文本，以及每个字符回原文的下标。"""
    chars: list[str] = []
    index: list[int] = []
    for position, ch in enumerate(text):
        if ch.isspace():
            continue
        chars.append(ch.translate(_LOOSE_PUNCT))
        index.append(position)
    return "".join(chars).lower(), index


def _slice_text(text: str, start_ratio: float, end_ratio: float) -> str:
    """按比例切分译文，并向最近的句末标点对齐，避免半句。"""
    length = len(text)
    if length == 0:
        return ""
    start = int(length * max(0.0, min(start_ratio, 1.0)))
    end = int(length * max(0.0, min(end_ratio, 1.0)))
    if end <= start:
        return ""
    for sep in ("。", "！", "？", "；", "\n"):
        pos = text.rfind(sep, max(0, start - 6), start)
        if pos >= 0:
            start = pos + 1
            break
    for sep in ("。", "！", "？", "；", "\n"):
        pos = text.find(sep, end, min(length, end + 6))
        if pos >= 0:
            end = pos + 1
            break
    return text[start:end].strip()


def _slice_translation(block: dict, page_index: int) -> str:
    """跨页块按 bboxes 的页片段高度比例切分译文，禁止整段每页写一遍。"""
    text = (block.get("translated") or "").strip()
    boxes = block.get("bboxes") or []
    if len(boxes) <= 1 or not text:
        return text
    ordered = sorted(boxes, key=lambda item: (int(item[0]), float(item[2])))
    heights = [max(0.0, float(item[4]) - float(item[2])) for item in ordered]
    total = sum(heights)
    if total <= 0:
        return text
    page_no = page_index + 1
    cursor = 0.0
    for box, height in zip(ordered, heights):
        if int(box[0]) == page_no:
            start_ratio = cursor / total
            end_ratio = (cursor + height) / total
            return _slice_text(text, start_ratio, end_ratio)
        cursor += height
    return ""


def _strip_heading_echo(
    text: str,
    page_items: list[tuple[dict, pymupdf.Rect]],
    block: dict,
) -> str:
    """去掉正文译文开头误抄进来的相邻标题整句。

    翻译模型会把相邻节标题一起翻进正文块，而标题本身也有一块会单独写，
    结果同一句中文在页面上出现两次（实测「双通路抑制的重要性」重复）。
    只在「本块译文以同页标题译文开头且后面还有正文」时剥离。
    """
    if block.get("kind") in ("heading", "title", "caption"):
        return text
    stripped = (text or "").strip()
    if not stripped:
        return text
    for other, _rect in page_items:
        if other is block:
            continue
        if other.get("kind") not in ("heading", "title", "caption"):
            continue
        prefix = (other.get("translated") or "").strip()
        if len(prefix) < 4 or not stripped.startswith(prefix):
            continue
        rest = stripped[len(prefix):].lstrip(" \t\r\n\u3000：:，,。;；")
        if len(rest) >= 8:
            return rest
    return text


def _tokenize_inline(text: str, inline_math: list[dict]) -> list[dict]:
    """把译文切成 text / math token；公式按 source 在译文中独立定位。"""
    matches: list[tuple[int, int, dict]] = []
    for item in inline_math or []:
        source = (item.get("source") or "").strip()
        if not source:
            continue
        chars = [ch for ch in source if not ch.isspace()]
        if not chars:
            continue
        pattern = re.compile(r"\s*".join(re.escape(ch) for ch in chars))
        hit = pattern.search(text)
        if hit:
            matches.append((hit.start(), hit.end(), item))
            continue
        # 译文常把 `qi= hiWQ` 写成 `qi = hiW Q`、把 `,` 写成 `，`；
        # 精确正则失败后做去空白 + 标点归一的宽松匹配。
        loose_text, loose_map = _loose_map(text)
        loose_source = "".join(ch for ch in source if not ch.isspace()).translate(
            _LOOSE_PUNCT
        ).lower()
        pos = loose_text.find(loose_source)
        if pos >= 0 and loose_source:
            end_pos = pos + len(loose_source) - 1
            if end_pos < len(loose_map):
                matches.append((loose_map[pos], loose_map[end_pos] + 1, item))
    matches.sort(key=lambda value: value[0])

    dedup: list[tuple[int, int, dict]] = []
    last = -1
    for start, end, meta in matches:
        if start >= last:
            dedup.append((start, end, meta))
            last = end

    tokens: list[dict] = []
    cursor = 0
    for start, end, meta in dedup:
        if start > cursor:
            tokens.append({"type": "text", "text": text[cursor:start]})
        tokens.append({"type": "math", "meta": meta})
        cursor = end
    if cursor < len(text):
        tokens.append({"type": "text", "text": text[cursor:]})
    return tokens


def _clean_segment(text: str) -> str:
    out = text
    for ch, rep in _SYMBOL_FALLBACK.items():
        if ch in out:
            out = out.replace(ch, rep)
    return re.sub(r"[ \t]{2,}", " ", out)


def _inline_atoms(
    font: pymupdf.Font,
    tokens: list[dict],
    size: float,
    pixmaps: dict[tuple, pymupdf.Pixmap],
) -> list[dict]:
    atoms: list[dict] = []
    for token in tokens:
        if token["type"] == "math":
            meta = token["meta"]
            bbox = meta.get("bbox")
            latex = str(meta.get("latex") or "") or source_to_latex(
                meta.get("source") or ""
            )
            source = str(meta.get("source") or "").strip()
            if not bbox and not latex and not source:
                continue
            # 旧产物里列表项目符号仍被当成行内公式，逐条裁图既难看又没必要。
            if source and all(ch in DECORATIVE_CHARS for ch in source):
                atoms.append(
                    {"type": "text", "text": "·", "w": font.text_length("·", size)}
                )
                continue
            pix = None
            if bbox:
                pix = pixmaps.get(tuple(round(float(v), 1) for v in bbox))
            rect = pymupdf.Rect(bbox) if bbox and len(bbox) >= 4 else None
            # 单行水平式按 1.05×字号；含上下标的 bbox 更高，按 1.18×字号，
            # 否则压缩后整图会偏上。
            if rect is not None and not rect.is_empty:
                height = size * (1.18 if rect.height > size * 1.25 else 1.05)
                width = height * (rect.width / max(rect.height, 1))
            else:
                height = size * 1.12
                width = max(font.text_length(source or "x", size), size * 0.6)
            # 裁图缺失也必须保留 token：先用 LaTeX，再退回原文符号，
            # 绝不能让公式从句子中间消失（缺 token 会变成半句中文）。
            atoms.append(
                {
                    "type": "math",
                    "pixmap": pix,
                    "latex": latex,
                    "source": source,
                    "w": width,
                    "h": height,
                }
            )
            continue
        text = _clean_segment(token["text"])
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch.isascii() and (ch.isalnum() or ch in _WORD_CHARS):
                j = i
                while (
                    j < n
                    and text[j].isascii()
                    and (text[j].isalnum() or text[j] in _WORD_CHARS)
                ):
                    j += 1
                word = text[i:j]
                atoms.append(
                    {"type": "text", "text": word, "w": font.text_length(word, size)}
                )
                i = j
                continue
            atoms.append({"type": "text", "text": ch, "w": font.text_length(ch, size)})
            i += 1
    return atoms


def _wrap_inline(
    atoms: list[dict], width: float, capacity: int
) -> tuple[list[list[dict]], bool]:
    lines: list[list[dict]] = []
    current: list[dict] = []
    used = 0.0
    for atom in atoms:
        if current and used + atom["w"] > width:
            lines.append(current)
            current = []
            used = 0.0
            if atom["type"] == "text" and atom["text"] == " ":
                continue
        current.append(atom)
        used += atom["w"]
    if current:
        lines.append(current)
    overflow = len(lines) > capacity
    return lines[:capacity], overflow


def _layout_inline(
    font: pymupdf.Font,
    tokens: list[dict],
    band: pymupdf.Rect,
    source_size: float,
    pixmaps: dict[tuple, pymupdf.Pixmap],
    *,
    min_size: float = DEFAULT_MIN_SIZE,
    line_height: float = DEFAULT_LINE_HEIGHT,
    min_line_height: float = DEFAULT_MIN_LINE_HEIGHT,
) -> tuple[float, float, list[list[dict]], bool]:
    size = source_size
    lh = line_height
    while size >= min_size:
        atoms = _inline_atoms(font, tokens, size, pixmaps)
        capacity = int((band.height + 0.5) // (size * lh))
        lines, overflow = _wrap_inline(
            atoms, max(band.width - 1, 8), max(capacity, 1)
        )
        if not overflow:
            return size, lh, lines, False
        if lh > min_line_height:
            lh = max(min_line_height, round(lh - 0.04, 3))
            continue
        lh = line_height
        size -= 0.25
    atoms = _inline_atoms(font, tokens, min_size, pixmaps)
    capacity = int((band.height + 0.5) // (min_size * min_line_height))
    lines, overflow = _wrap_inline(
        atoms, max(band.width - 1, 8), max(capacity, 1)
    )
    return min_size, min_line_height, lines, overflow


def _write_justified_line(
    page: pymupdf.Page,
    x0: float,
    baseline: float,
    line: str,
    font: pymupdf.Font,
    fontname: str,
    size: float,
    avail_width: float,
) -> None:
    """中文行两端对齐：把剩余宽度均分到字符间隙。

    只对「非段末行 + 行明显没写满 + 含中文」的行生效；段末行保持左对齐，
    否则末行会被拉成夸张的字距。
    """
    chars = list(line)
    if len(chars) < 2:
        page.insert_text(
            (x0, baseline), line, fontname=fontname, fontsize=size, color=(0, 0, 0)
        )
        return
    widths = [font.text_length(ch, size) for ch in chars]
    total = sum(widths)
    gap = (avail_width - total) / (len(chars) - 1)
    if gap <= 0.05:
        page.insert_text(
            (x0, baseline), line, fontname=fontname, fontsize=size, color=(0, 0, 0)
        )
        return
    x = x0
    for ch, width in zip(chars, widths):
        if not ch.isspace():
            page.insert_text(
                (x, baseline), ch, fontname=fontname, fontsize=size, color=(0, 0, 0)
            )
        x += width + gap


def _x_overlap_ratio(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = min(a.x1, b.x1) - max(a.x0, b.x0)
    if inter <= 0:
        return 0.0
    return inter / max(min(a.width, b.width), 1.0)


def _merge_paragraph_fragments(
    targets: list[tuple[dict, pymupdf.Rect]],
    body_size: float,
    page_words: list[tuple] | None = None,
) -> tuple[list[tuple[dict, pymupdf.Rect]], set[int]]:
    """同栏相邻的正文碎片合并成一个段落目标（块级排版准确性）。

    解析把长段落拆成多个「首行 bbox」（高约 10pt）时，逐块回填会让同一段在
    页面上碎成多截、中英交错。这里把同页、同 kind、横向重叠 >=60%、纵向
    间隙 <= 1.6 行高的连续块合并：文本按 y 拼接、rect 取并集，只写一次译文。

    返回 (合并后的 targets, 被吸收块的 id 集合)。被吸收的块要从 page_blocks
    里剔除，否则 next_limit / 文字范围计算仍会被它们截断。
    """
    if not targets:
        return targets, set()
    gap_limit = max(body_size * 1.6, 8.0)
    groups: list[list[tuple[dict, pymupdf.Rect]]] = []
    for block, rect in targets:
        kind = block.get("kind") or ""
        if kind not in ("para", "abstract"):
            groups.append([(block, rect)])
            continue
        merged = False
        for group in groups:
            last_block, last_rect = group[-1]
            if last_block.get("page") != block.get("page"):
                continue
            if last_block.get("kind") != kind:
                continue
            if _x_overlap_ratio(last_rect, rect) < 0.6:
                continue
            if rect.y0 - last_rect.y1 > gap_limit or rect.y0 < last_rect.y0:
                continue
            group.append((block, rect))
            merged = True
            break
        if not merged:
            groups.append([(block, rect)])

    out: list[tuple[dict, pymupdf.Rect]] = []
    absorbed: set[int] = set()
    for group in groups:
        if len(group) == 1:
            out.append(group[0])
            continue
        group.sort(key=lambda item: (item[1].y0, item[1].x0))
        base = dict(group[0][0])
        texts = [
            (item[0].get("text") or "").strip()
            for item in group
            if (item[0].get("text") or "").strip()
        ]
        translated = [
            (item[0].get("translated") or "").strip()
            for item in group
            if (item[0].get("translated") or "").strip()
        ]
        base["text"] = " ".join(texts)
        base["translated"] = " ".join(translated)
        base["inline_math"] = [
            math
            for item in group
            for math in (item[0].get("inline_math") or [])
        ]
        rect = pymupdf.Rect(group[0][1])
        for _block, other in group[1:]:
            rect |= other
        # 合并组的下界用真实文字底部（words），不用 bbox 并集：解析的 bbox
        # 常只盖首行，按下界下推会让后块与前块的实际文字相交。
        group_ids = {id(item[0]) for item in group}
        below = [
            other_rect.y0
            for other_block, other_rect in targets
            if id(other_block) not in group_ids
            and other_block.get("page") == group[0][0].get("page")
            and _x_overlap_ratio(rect, other_rect) >= 0.3
            and other_rect.y0 >= rect.y1 - 1
        ]
        limit = min(below) - 1 if below else rect.y1 + body_size * 4
        real_bottom = rect.y1
        for word in page_words or []:
            _wx0, _wy0, _wx1, _wy1 = (float(value) for value in word[:4])
            center = (_wx0 + _wx1) / 2
            if rect.x0 - 2 <= center <= rect.x1 + 2 and _wy1 <= limit:
                real_bottom = max(real_bottom, _wy1)
        rect = pymupdf.Rect(rect.x0, rect.y0, rect.x1, real_bottom)
        for _block, _other in group[1:]:
            absorbed.add(id(_block))
        out.append((base, rect))
    return out, absorbed


def render_page(
    pdf_path: Path | str,
    blocks: list[dict],
    page_index: int,
    out_pdf: Path | str | None = None,
    *,
    font_path: Path | str | None = None,
    heading_font_path: Path | str | None = None,
    body_size: float = BODY_FONT_SIZE,
    min_size: float = DEFAULT_MIN_SIZE,
    max_size: float = DEFAULT_MAX_SIZE,
    line_height: float = DEFAULT_LINE_HEIGHT,
    skip_nested_math: bool = False,
    dry_run: bool = False,
    sidebar_size: float | None = None,
) -> PageRenderResult:
    """把一页的已译块写回原版面。``out_pdf`` 为单页 PDF 输出路径。"""
    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    font_file = Path(font_path) if font_path else pick_body_font()
    font = load_font(font_file)
    heading_file = Path(heading_font_path) if heading_font_path else pick_heading_font()
    heading_font = load_font(heading_file)

    page_size = (page.rect.width, page.rect.height)
    result = PageRenderResult(page=page_index, dry_run=dry_run, page_size=page_size)
    result.images_before = len(page.get_images(full=True))
    result.drawings_before = len(page.get_drawings())

    page_blocks: list[tuple[dict, pymupdf.Rect]] = []
    keep_blocks: list[tuple[dict, pymupdf.Rect]] = []
    for block in blocks:
        rect = _rect(block, page_index)
        if rect is None or rect.is_empty:
            continue
        page_blocks.append((block, rect))
        if block.get("kind") in KEEP_KINDS:
            keep_blocks.append((block, rect))

    # 页面级公式禁区：公式与图表一样不可写。不能只看「本块自己的公式」，
    # 否则别的块写中文时会绕不开相邻块的公式区（实测第 7 页中文压公式）。
    page_math_rects: list[pymupdf.Rect] = []
    for _mb, _mr in page_blocks:
        if (_mb.get("kind") or "") == "equation":
            page_math_rects.append(pymupdf.Rect(_mr))
        for _item in _mb.get("inline_math") or []:
            _bb = _item.get("bbox")
            if _bb and len(_bb) >= 4:
                page_math_rects.append(pymupdf.Rect(_bb))

    # L1 区域层：先认页面上的页缘信息栏。侧栏是独立区域，有自己的字号与
    # 排版范围；正文块一律不得写进侧栏列，侧栏块也不得越回正文列。
    sidebar_bands = (
        _detect_sidebar_bands(
            page_blocks,
            page.rect.width,
            [other for _kind_block, other in keep_blocks],
        )
        if env_sidebar_translate()
        else []
    )
    # 解析层已把首页窄栏块标为 sidebar，但本页几何检测可能漏掉这条只有
    # 3–4 行的短列。用解析结果的横向范围补一条列边界，侧栏块才能原地排版；
    # 否则它们会全部走 sidebar_outside，整栏保留英文。
    if not sidebar_bands:
        parsed_side = [
            rect for block, rect in page_blocks
            if block.get("column_role") == "sidebar"
        ]
        if parsed_side:
            side_x0 = min(rect.x0 for rect in parsed_side)
            side_x1 = max(rect.x1 for rect in parsed_side)
            if 8 < side_x1 - side_x0 < page.rect.width * 0.4:
                sidebar_bands = [
                    pymupdf.Rect(
                        max(0.0, side_x0 - 6.0),
                        0.0,
                        min(page.rect.width, side_x1 + 6.0),
                        page.rect.height,
                    )
                ]
    result.sidebar_bands = [(band.x0, band.x1) for band in sidebar_bands]
    if sidebar_size is None:
        sidebar_size = min(body_size, SIDEBAR_MAX_SIZE)
    # 侧栏字号不得超过同页正文：侧栏是窄列（首页实测只有 76pt 宽），字号比
    # 正文还大时中文一行放不下两三个词，折行后必然越出列界写进正文列。
    sidebar_size = max(
        SIDEBAR_MIN_SIZE,
        min(float(sidebar_size), SIDEBAR_MAX_SIZE, float(body_size)),
    )
    # 再按窄列宽度封顶：中文没有空格，不会被折行算法断开，字号一大整行
    # 直接顶出列界。按「一行最多 10 个汉字」估宽，给列界留安全余量。
    if sidebar_bands:
        narrow = min(band.x1 - band.x0 for band in sidebar_bands)
        if narrow > 0:
            sidebar_size = max(
                SIDEBAR_MIN_SIZE, min(sidebar_size, narrow / 10.0)
            )
    role_by_block: dict[int, str] = {}
    # 图区（位图 / 矢量簇 / figure 块）：落在其中的文字是图内标注。短标签
    # 保留原文，成句说明照常翻译，但严格写回各自 bbox，不合并、不扩栏。
    graphic_regions = page_graphic_regions(page, page_blocks)
    graphic_page = page_is_graphic_page(page, page_blocks, body_size)
    figure_text_ids: set[int] = set()

    targets: list[tuple[dict, pymupdf.Rect]] = []
    for block, rect in page_blocks:
        kind = block.get("kind") or ""
        if kind not in BODY_KINDS:
            continue
        if not block.get("translatable") or not (block.get("translated") or "").strip():
            continue
        if is_same_text(block):
            result.skipped.append({"block_id": block.get("id"), "reason": "same_text"})
            continue

        if is_math_like(block):
            result.skipped.append({"block_id": block.get("id"), "reason": "math_like"})
            continue
        if is_formula_echo(block):
            result.skipped.append(
                {"block_id": block.get("id"), "reason": "formula_echo"}
            )
            continue
        # 题注被解析进了表格/图片 bbox：擦它会连带擦掉表格数据行，
        # 这时保留原文，表格完整性优先（阅读流里仍有题注译文）。
        # 只对「落在表格框内」的题注跳过回填：擦它确实会连表体数据行一起
        # 擦掉（实测表格词数 84→76）。figure 的 bbox 常把图注一起罩住，但图
        # 本身是位图、redact 不会删图，图注照常擦写是安全的——此前把 figure
        # 也算进来，导致 Figure 2/4 的图注一直保留英文。
        if kind == "caption" and any(
            _kb.get("kind") == "table"
            and not (rect & other).is_empty
            and (rect & other).get_area() > rect.get_area() * 0.5
            for _kb, other in keep_blocks
        ):
            result.skipped.append(
                {"block_id": block.get("id"), "reason": "caption_in_table"}
            )
            continue
        # 高不足一行的块基本是图内标注/页脚碎片（实测 5.3pt 的
        # "DiffSBDD, generated"），任何字号都放不下，译了只会变成溢出。
        if (
            block.get("kind") in ("para", "caption", "heading")
            and (rect.height < 6 or (rect.width < 20 and rect.height < 16))
        ):
            result.skipped.append(
                {"block_id": block.get("id"), "reason": "too_small"}
            )
            continue
        if skip_nested_math and _contains_inner_math(rect, keep_blocks):
            result.skipped.append({"block_id": block.get("id"), "reason": "nested_math"})
            continue
        # 图内文字：短标签不译不擦（"AI"/"Worker"/"Research plan" 翻译无收益，
        # 窄框里折行还会压住图形）；成句说明继续翻译，但走严格原位回填。
        if kind != "caption" and (
            graphic_page or _inside_graphic_region(rect, graphic_regions)
        ):
            if _looks_like_figure_label(str(block.get("text") or "")):
                result.skipped.append(
                    {"block_id": block.get("id"), "reason": "figure_label"}
                )
                continue
            # 同一行并排多个标签被并成一块：共同 bbox 横跨多个框，译文必然
            # 错位；整块保留原文（不擦不写），与「擦哪里写哪里」一致。
            if _figure_text_has_gaps(page, rect):
                result.skipped.append(
                    {"block_id": block.get("id"), "reason": "figure_multi_label"}
                )
                continue
            figure_text_ids.add(id(block))
            targets.append((block, rect))
            continue
        # 页底脚注 / 作者单位：位置 + 源字号两条判据，保留原文不擦。
        if _is_page_footnote(page, block, rect, page.rect.height, body_size):
            result.skipped.append(
                {"block_id": block.get("id"), "reason": "page_footnote"}
            )
            continue
        side_band = _in_sidebar(rect, sidebar_bands)
        if side_band is None and block.get("column_role") == "sidebar":
            # 解析层判为页缘信息栏，但本页几何检测没识别出这条窄栏。这类块
            # （通讯作者、ORCID、citation 收尾）多是零散短行，当中文正文写
            # 会铺到正文列上；保留原文并记账，不在排版层做二次猜测。
            result.skipped.append(
                {"block_id": block.get("id"), "reason": "sidebar_outside"}
            )
            continue
        if (
            side_band is not None
            and not _looks_like_prose(block)
            and _looks_like_person_name(str(block.get("text") or ""))
        ):
            # 人名白名单：不擦不写，也不计入可译分母。
            result.skipped.append(
                {"block_id": block.get("id"), "reason": "sidebar_name"}
            )
            continue
        if side_band is not None and not _looks_like_prose(block):
            # 侧栏自己参与翻译与回填（不再是「跳过保留原文」），列区间是硬边界。
            role_by_block[id(block)] = "sidebar"
            rect = pymupdf.Rect(
                max(rect.x0, side_band.x0 + 0.5),
                rect.y0,
                # 右界再收 2pt：全角字符（《》、「」）的字宽略大于字号，
                # 贴着列界排会有一两个字形溢出到正文列，被跨列泄漏判据抓到。
                min(rect.x1, side_band.x1 - 2.0),
                rect.y1,
            )
            targets.append((block, rect))
            continue
        # 正文块横跨到侧栏列：旧产物里这类块（单块覆盖整页）无法判定其真实
        # 归属，收窄只会把一半中文写进侧栏。按 L3 口径整块保留原文并记账。
        hit_band = next(
            (
                band
                for band in sidebar_bands
                if _overlaps_x(rect, band)
                and (rect & band).width > 1.5
                and (rect & band).height > 1.5
            ),
            None,
        )
        if hit_band is not None:
            if hit_band.x0 <= rect.x0:
                body_rect = pymupdf.Rect(hit_band.x1 + 1.0, rect.y0, rect.x1, rect.y1)
            else:
                body_rect = pymupdf.Rect(rect.x0, rect.y0, hit_band.x0 - 1.0, rect.y1)
            body_ratio = body_rect.width / max(rect.width, 1)
            # 侧栏只占边缘、正文侧仍是主体 -> 收窄到正文列照常翻译；
            # 只有正文侧不足 60% 时才放弃整块（说明它主要是信息栏内容）。
            if body_ratio < 0.6 or body_rect.width < 40:
                result.skipped.append(
                    {"block_id": block.get("id"), "reason": "sidebar_conflict"}
                )
                continue
            rect = body_rect
        targets.append((block, rect))
    result.sidebar_blocks = sum(
        1 for role in role_by_block.values() if role == "sidebar"
    )

    # 图内文字互相压框：整页系统图的标签常被解析成多个 bbox 交叉的独立块
    # （同一行上「左侧说明 + 中间框标题」各成一块）。各写各的中文会叠字，
    # 而原位约束又不允许下推位移，因此这类冲突组整组保留原文（不擦不写）。
    if env_in_place_strict() and figure_text_ids:
        figure_targets = [
            (block, rect) for block, rect in targets if id(block) in figure_text_ids
        ]
        conflicted: set[int] = set()
        for index, (first, first_rect) in enumerate(figure_targets):
            for second, second_rect in figure_targets[index + 1:]:
                dx = min(first_rect.x1, second_rect.x1) - max(
                    first_rect.x0, second_rect.x0
                )
                dy = min(first_rect.y1, second_rect.y1) - max(
                    first_rect.y0, second_rect.y0
                )
                if dx <= 0:
                    continue
                # 纵向相交（含几乎贴住）且横向重叠：两段中文必然叠在同一行。
                # 图内标签常只差 1–3pt，按面积比判会漏，这里按「相交即冲突」。
                if dy > -2.0 and dx > min(first_rect.width, second_rect.width) * 0.2:
                    conflicted.add(id(first))
                    conflicted.add(id(second))
        if conflicted:
            kept: list[tuple[dict, pymupdf.Rect]] = []
            for block, rect in targets:
                if id(block) in conflicted:
                    result.skipped.append(
                        {"block_id": block.get("id"), "reason": "figure_overlap"}
                    )
                    continue
                kept.append((block, rect))
            targets = kept
            figure_text_ids -= conflicted

    # 短标签（节标题/小标题）先占位，长段落再避让：长段先写时短标签已无处
    # 可下推，只能压在长段首行上（Abstract 标题与摘要正文即此形态）。
    targets.sort(
        key=lambda item: (
            item[1].height > 16,
            # 短块组内必须按 y 升序：只按 order 排会让下方块先写，
            # 上方块后写时不检查下方已排块，两行直接叠在一起（p3/p18）。
            round(item[1].y0, 1),
            int(item[0].get("order") or 0),
        )
    )

    # 每个目标块：向下借用同栏空隙，再把公式/图表当成禁排区切成横带。
    # 只擦横带内的文字，公式本身既不被擦除，也不会被译文压住。
    bands_by_block: dict[int, list[pymupdf.Rect]] = {}
    redact_bands_by_block: dict[int, list[pymupdf.Rect]] = {}
    obstacles_by_block: dict[int, list[pymupdf.Rect]] = {}
    layout_rects: dict[int, pymupdf.Rect] = {}
    text_rects_by_block: dict[int, list[pymupdf.Rect]] = {}
    # 已处理块的「原始 bbox」：下推只按真实文字范围算，不能用被 next_limit
    # 向下扩过的 layout_rect，否则会把后块推得过远、引发新的溢出。
    # 块级准确性：解析常把一段拆成多个首行 bbox，逐块回填会让同一段在页面上
    # 碎成多截。targets 填充完成后，把同栏相邻碎片合并成一个段落目标。
    # 严格原位模式不合并：合并会把多个块的 rect 取并集、跨越各自的原位置，
    # 与「擦哪里写哪里」互斥（图内标签被并成一大段即此形态）。
    if env_layout_strict() and not env_in_place_strict():
        targets, _absorbed = _merge_paragraph_fragments(
            targets, body_size, page.get_text("words")
        )
        if _absorbed:
            page_blocks = [
                (block, rect) for block, rect in page_blocks
                if id(block) not in _absorbed
            ]
    placed_rects: list[pymupdf.Rect] = []

    for block, rect in targets:
        block_role = role_by_block.get(id(block), "body")
        limit = next_limit(rect, page_blocks, page.rect.height)
        # 短标签只有一行，向下借用会把下方整段算进自己的范围，后块因此
        # 无处下推（Abstract 标题压住摘要正文即此形态）。侧栏是逐行紧排，
        # 下一行就是相邻块，同样不得向下借空隙，否则折行会压住下一行原文。
        # 「短标签」按文本长度判定，而不是只看块高：解析常把长段落压成
        # 首行 bbox（10pt 高），若按高度一律当短标签，就禁止向下借空隙，
        # 可用高度只剩 11pt、整段永远排不下 → 整块 preserved 留英文。
        short_label = rect.height <= 16 and len((block.get("text") or "").strip()) <= 40
        if block_role == "sidebar":
            limit = rect.y1
        elif short_label:
            # 短标签只有一行，但中文可能更长：差一行以内时允许向下借一行
            # 空隙（不越过同栏下一块），不因此把整块退回保留英文。
            limit = min(
                rect.y1 + body_size * line_height,
                next_limit(rect, page_blocks, page.rect.height),
            )
        # 解析 bbox 有时只盖段落首行（b0046 400–486 实际到 619）；
        # 用该块所在 x 范围内文字的实际最大 y 补足，但不得越过下一段非图表块。
        words_max = rect.y1
        dominant: tuple[float, float] | None = None
        if block.get("kind") in ("para", "caption") and block_role != "sidebar":
            next_non_keep = min(
                [
                    other.y0
                    for keep_block, other in page_blocks
                    if keep_block.get("kind") not in KEEP_KINDS
                    and other.y0 >= rect.y1 - 2
                    and _overlaps_x(rect, other)
                    # 跨侧栏的块最终会被 sidebar_conflict 剔除、本就不参与
                    # 回填，不能拿它的上沿当本块文字范围的上界：否则长段落
                    # 被截成「首行 10pt」，只擦首行、整段英文留在页面上。
                    and not any(
                        _overlaps_x(other, band) and not (other & band).is_empty
                        for band in sidebar_bands
                    )
                ]
                + [page.rect.height - PAGE_BOTTOM_MARGIN]
            )
            words = page.get_text(
                "words",
                clip=pymupdf.Rect(rect.x0, rect.y0, rect.x1, next_non_keep),
            )
            if words:
                words_max = max(float(word[3]) for word in words)
                dominant = _dominant_column(words)
        # P2 块几何契约：新解析产物带 text_rects，块的可写范围以实际文字行
        # 为准，不再用「跨栏收窄 / 短标签扩栏 / 下推」补偿上游几何。
        block_rects = [
            pymupdf.Rect(float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in (block.get("text_rects") or [])
            if len(r) >= 5 and int(r[0]) == page_index + 1
        ]
        has_text_rects = bool(block_rects) and env_block_strict()

        # 跨栏块（左窄栏 + 右正文被解析成一块）按 words 的主体栏收窄，
        # 否则译文横跨两栏、压到作者单位 / 期刊信息栏上。
        if rect.width > page.rect.width * 0.45 and dominant and not has_text_rects:
            dom_x0, dom_x1 = dominant
            # 收窄后宽度不得小于原宽 60%：首页「单栏正文 + 左侧信息栏」的文字
            # 密度沿 x 平坦，窄峰会伪造成「主体栏」，把正文压成 14–32pt 一条，
            # 折行后每行只剩 1–2 字、永远排不下，整块被迫 preserved 留英文。
            if dom_x1 - dom_x0 >= rect.width * 0.6 and dom_x1 - dom_x0 < rect.width - 40:
                rect = pymupdf.Rect(
                    max(rect.x0, dom_x0), rect.y0, min(rect.x1, dom_x1), rect.y1
                )
        if has_text_rects:
            # 契约模式：只用实际文字行的上下界，不向下一块借高度、不猜首行。
            text_top = min(r.y0 for r in block_rects)
            text_bottom = max(r.y1 for r in block_rects)
            layout_rect = pymupdf.Rect(
                rect.x0,
                min(rect.y0, text_top),
                rect.x1,
                min(text_bottom + 0.5, page.rect.height - PAGE_BOTTOM_MARGIN),
            )
        else:
            layout_rect = pymupdf.Rect(
                rect.x0,
                rect.y0,
                rect.x1,
                max(
                    limit,
                    min(
                        words_max + 1,
                        page.rect.height - PAGE_BOTTOM_MARGIN,
                    ),
                ),
            )
            if env_in_place_strict():
                # 擦除范围 = 该块实际文字范围（word 并集补足首行 bbox），上限是
                # 同栏下一块的上沿和页底保留区。不能用 next_limit 把范围撑到
                # 下一块之前的大片空白——那会「擦掉不属于本块的地方」。
                natural_bottom = max(rect.y1, words_max + 1)
                if short_label:
                    natural_bottom = max(
                        natural_bottom, rect.y1 + body_size * line_height
                    )
                layout_rect = pymupdf.Rect(
                    rect.x0,
                    rect.y0,
                    rect.x1,
                    min(
                        limit,
                        natural_bottom,
                        page.rect.height - PAGE_BOTTOM_MARGIN,
                    ),
                )
        role = block_role
        # 小标题/短标签的 bbox 往往只有文字自身宽度（"B. GNNs" 仅 37.9pt），
        # 译文稍长就溢出，被擦掉后原地留白。右侧同栏为空时扩到同栏右缘。
        # 侧栏块不扩：列宽是硬边界，不得越回正文列。
        # 图内文字一律不扩：图区里的空位不是它的排版范围，扩栏会把中文推到
        # 图形之外（整页系统图的标签被拉成整行即此形态）。
        if (
            role != "sidebar"
            and id(block) not in figure_text_ids
            and not has_text_rects
            and layout_rect.height <= 16
            and rect.width < page.rect.width * 0.5
        ):
            right = max(
                [rect.x1]
                + [
                    other.x1
                    for _kb, other in page_blocks
                    if abs(other.x0 - rect.x0) < 12
                ]
            )
            probe = pymupdf.Rect(rect.x1, rect.y0 - 1, right, rect.y1 + 1)
            if right > rect.x1 + 4 and not page.get_text("words", clip=probe):
                layout_rect = pymupdf.Rect(rect.x0, rect.y0, right, layout_rect.y1)
        # 解析常把节标题从正文段落里拆成独立块，但两块 bbox 仍共享首行
        # （典型：Abstract 标题压在摘要正文第一行上）。按阅读顺序把后块的
        # 起点推到前块之下，保证任何两段中文都不会写在同一行。
        # 图内标签不参与下推：它们彼此只差 1–5pt，任何下推都会连锁把后面
        # 的标签一路推开，中文因此越出原框、盖住整张图。
        # 契约模式仍允许「最小避让」：前块占位取 text_rects 的真实底部，
        # 只在下推能避免叠字时移动，不再用 bbox 推测大范围。
        if id(block) not in figure_text_ids:
            shift_y0 = layout_rect.y0
            for prev_rect in placed_rects:
                if (
                    _overlaps_x(prev_rect, layout_rect)
                    and prev_rect.y0 <= layout_rect.y0 + 0.5
                    and prev_rect.y1 > shift_y0
                ):
                    # 留 3pt：标题字号大于正文时 descender 会超出原 bbox，
                    # 只让 1pt 会让两行仍然贴在一起（实测重叠 0.4pt）。
                    shift_y0 = prev_rect.y1 + 3.0
            if shift_y0 > layout_rect.y0 + 0.5:
                if shift_y0 < layout_rect.y1 - 2:
                    layout_rect = pymupdf.Rect(
                        layout_rect.x0, shift_y0, layout_rect.x1, layout_rect.y1
                    )
                else:
                    # 下推后原地已没有空间：向下扩到同栏下一块，靠缩字号放下；
                    # 不能放弃下推，否则与前块直接重叠（p3 / p18 实测）。
                    room = next_limit(
                        pymupdf.Rect(
                            layout_rect.x0, shift_y0, layout_rect.x1, layout_rect.y1
                        ),
                        page_blocks,
                        page.rect.height,
                    )
                    if room > shift_y0 + 1:
                        layout_rect = pymupdf.Rect(
                            layout_rect.x0, shift_y0, layout_rect.x1, room
                        )
        # 存实际排版范围而不是原始 bbox：后续块要按前块真实结束位置下推，
        # 用 bbox 会把「首行 bbox」当成整段范围，导致下推不足而重叠。
        if has_text_rects:
            text_rects_by_block[id(block)] = block_rects
        placed_rects.append(layout_rect)

        obstacles = [
            _inflate(pymupdf.Rect(other))
            for _kb, other in keep_blocks
            # figure 的 bbox 常把图注一起罩住（旧产物尤其明显），图片本身
            # 不会被 redact 擦除，不需要作为文字禁排区。
            if _kb.get("kind") != "figure"
            and not (pymupdf.Rect(other) & layout_rect).is_empty
        ]
        # 公式与图表一样是不可写区域：页面级公式禁区全部加入本块禁排区，
        # 中文只在公式之外绕排；放不下由字号适配，绝不允许压公式。
        own_math = []
        for item in block.get("inline_math") or []:
            bbox = item.get("bbox")
            if bbox and len(bbox) >= 4:
                own_math.append(pymupdf.Rect(bbox))
        for r in page_math_rects:
            if (r & layout_rect).is_empty:
                continue
            # 本块自己的公式由 token 流消费，不能再当禁排区，否则整个块
            # 会因为没有可用行而退回纯文本。
            if any(abs(r.x0 - own.x0) < 1.5 and abs(r.y0 - own.y0) < 1.5 for own in own_math):
                continue
            obstacles.append(_inflate(r))
        bands_by_block[id(block)] = plan_bands(layout_rect, obstacles)
        # 擦除用全部横带（含窄带），排版仍只用宽横带
        redact_bands_by_block[id(block)] = plan_bands(
            layout_rect, obstacles, min_width_ratio=0.0, min_height=0.5
        )
        obstacles_by_block[id(block)] = obstacles
        layout_rects[id(block)] = layout_rect
        # 越界断言：侧栏块必须留在本列，正文块不得落进侧栏列（两者都为硬线）。
        if role == "sidebar":
            if _in_sidebar(layout_rect, sidebar_bands) is None:
                result.cross_leaks.append(str(block.get("id")))
        else:
            for band in sidebar_bands:
                inter = layout_rect & band
                if not inter.is_empty and inter.width > 1.5 and inter.height > 1.5:
                    result.cross_leaks.append(str(block.get("id")))
                    break

    inline_pixmaps: dict[int, dict[tuple, pymupdf.Pixmap]] = {}

    if not dry_run:
        # 行内公式裁图必须在 redact 之前完成：apply_redactions 会擦掉原字形。
        for block, rect in targets:
            math_items = block.get("inline_math") or []
            if not math_items:
                continue
            layout_rect = layout_rects[id(block)]
            pixmaps: dict[tuple, pymupdf.Pixmap] = {}
            for item in math_items:
                bbox = item.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                r = pymupdf.Rect(bbox)
                if (r & layout_rect).is_empty:
                    continue
                pixmaps[tuple(round(float(v), 1) for v in bbox)] = page.get_pixmap(
                    clip=r, matrix=pymupdf.Matrix(4, 4), alpha=False
                )
            if pixmaps:
                inline_pixmaps[id(block)] = pixmaps
        preserved_ids: set[int] = set()

        planned: dict[int, tuple] = {}
        # 预排版：放不下就整块保留原文，不许「擦一半、写一半」。
        for block, rect in targets:
            page_text = _strip_heading_echo(
                _slice_translation(block, page_index), page_blocks, block
            )
            kind = block.get("kind")
            block_role = role_by_block.get(id(block), "body")
            if block_role == "sidebar":
                # 侧栏列用自己的字号档，与正文页级字号互不影响。
                size = sidebar_size
                use_font = font
                block_min = SIDEBAR_MIN_SIZE
            elif kind == "title":
                size = round(body_size * TITLE_RATIO, 2)
                use_font = heading_font
                block_min = min_size
            elif kind == "heading":
                size = round(
                    body_size
                    * HEADING_RATIOS.get(int(block.get("level") or 1), 1.12),
                    2,
                )
                use_font = heading_font
                # 标题字号不得超过自身 bbox 高度：解析给的一行标题 bbox 只有
                # 10–13pt，按比例放大后字形溢出，压到上下相邻行（p3/p18 实测）。
                size = min(
                    size,
                    max(
                        min_size,
                        (rect.height - 0.5)
                        / max(use_font.ascender - use_font.descender, 1.0),
                    ),
                )
                block_min = min_size
            else:
                size = _figure_text_size(
                    page, block, rect, figure_text_ids, body_size
                )
                use_font = font
                block_min = (
                    HARD_MIN_SIZE
                    if id(block) in figure_text_ids
                    else BODY_FLOOR_SIZE
                )
            bands = bands_by_block[id(block)]
            if (
                len(bands) == 1
                and inline_pixmaps.get(id(block))
                and _inline_math_ok(block, size)
            ):
                tokens = _tokenize_inline(
                    page_text, block.get("inline_math") or []
                )
                _isize, ilh, ilines, inline_overflow = _layout_inline(
                    use_font,
                    tokens,
                    bands[0],
                    size,
                    inline_pixmaps[id(block)],
                    min_size=block_min,
                    line_height=line_height,
                )
                planned[id(block)] = (size, use_font, ilh, ilines, True)
                overflow = inline_overflow
            else:
                text = clean_translation(
                    page_text, block.get("inline_math")
                )
                _lsize, llh, placements, remaining = layout_in_rows(
                    use_font,
                    text,
                    layout_rect,
                    obstacles,
                    size,
                    min_size=block_min,
                    max_size=max(max_size, size),
                    line_height=line_height,
                )
                planned[id(block)] = (size, use_font, llh, placements, False)
                overflow = bool(remaining.strip())
            if overflow:
                preserved_ids.add(id(block))
        # 同栏前块的中文常比原 bbox 短，块内下方留下空白（实测 p7 左栏
        # 三块各富余 17/70/97pt），而紧随其后的块却因空间不足保留英文。
        # 放不下的块向上借用同栏前一块的富余，再重排一次。
        # 严格原位模式禁用：向上借用会把块从自己的位置搬到前块的下方，
        # 用户要求「擦哪里写哪里」，位移即违约。放不下就按字号下限收，
        # 仍放不下整块保留原文并记账。

        for block, rect in targets:
            if env_in_place_strict():
                break
            if id(block) not in preserved_ids:
                continue
            if role_by_block.get(id(block)) == "sidebar":
                # 侧栏逐行紧排、且上下夹着未译的作者/单位块，借用前块余量
                # 会把译文拉到别行上（实测压进 affiliation 英文行）。侧栏
                # 一律保持原 y0，放不下就整块 preserved。
                continue
            if planned[id(block)][4]:
                continue
            layout_rect = layout_rects[id(block)]
            above = [
                (_b, _r)
                for _b, _r in targets
                if _b is not block
                and abs(_r.x0 - rect.x0) < 12
                and _r.y1 <= rect.y0 + 1
                and id(_b) not in preserved_ids
                and not planned[id(_b)][4]
                and planned[id(_b)][3]
            ]
            if not above:
                continue
            prev_block, _prev_rect = max(above, key=lambda item: item[1].y1)
            psize, _pfont, plh, pplacements, _pinline = planned[id(prev_block)]
            last_band, last_lines = pplacements[-1]
            prev_bottom = last_band.y0 + len(last_lines) * psize * plh
            new_y0 = prev_bottom + 2.0
            if new_y0 >= layout_rect.y0 - 1:
                continue
            new_rect = pymupdf.Rect(rect.x0, new_y0, rect.x1, layout_rect.y1)
            # 借用区间不得压到任何同列已排块的实际文字区间：拆分后的正文
            # 片段向上借用前块余量时，容易跨过中间块压到其末行（实测 p1
            # 出现 4pt 重叠）。冲突则放弃借用，交给降级链处理。
            blocked = False
            for other_block, other_rect in targets:
                if other_block is block or not _overlaps_x(other_rect, new_rect):
                    continue
                other_plan = planned.get(id(other_block))
                if not other_plan or not other_plan[3]:
                    continue
                o_size, _o_font, o_lh, o_placements, _o_inline = other_plan
                o_band, o_lines = o_placements[-1]
                o_bottom = o_band.y0 + len(o_lines) * o_size * o_lh
                if o_bottom > new_y0 + 1 and other_rect.y0 < layout_rect.y1 - 1:
                    blocked = True
                    break
            if blocked:
                continue
            new_obstacles = [
                _inflate(pymupdf.Rect(other))
                for _kb, other in keep_blocks
                if _kb.get("kind") != "figure"
                and not (pymupdf.Rect(other) & new_rect).is_empty
            ]
            new_bands = plan_bands(new_rect, new_obstacles)
            text = clean_translation(
                _slice_translation(block, page_index), block.get("inline_math")
            )
            size, use_font, _plh, _plines, _pinl = planned[id(block)]
            _nsize, nlh, nplacements, remaining = layout_in_bands(
                use_font,
                text,
                new_bands,
                size,
                min_size=(
                    SIDEBAR_MIN_SIZE
                    if role_by_block.get(id(block)) == "sidebar"
                    else BODY_FLOOR_SIZE
                ),
                max_size=max(max_size, size),
                line_height=line_height,
            )
            if remaining.strip():
                continue
            preserved_ids.discard(id(block))
            bands_by_block[id(block)] = new_bands
            redact_bands_by_block[id(block)] = plan_bands(
                new_rect, new_obstacles, min_width_ratio=0.0, min_height=0.5
            )
            layout_rects[id(block)] = new_rect
            obstacles_by_block[id(block)] = new_obstacles

            planned[id(block)] = (size, use_font, nlh, nplacements, False)
        for block, rect in targets:
            if id(block) in preserved_ids:
                continue
            # 用 layout_rect（从段首到下一块上沿）的横带擦除，而不是窄 bbox。
            # 解析出的 bbox 有时只覆盖段落首行（实测 b0083 只有 10pt，段落实际
            # 245pt），只擦 bbox 会留下整段英文，译文再写上去就是中英重叠。
            block_redact_rects = text_rects_by_block.get(id(block)) or []
            if block_redact_rects:
                # 契约模式：擦除范围 = text_rects 的外接矩形，不向下一块扩展。
                # 逐行擦会漏掉行间的孤立公式符号（实测 p5/p9 的 ×/=/s），
                # 这些符号留在页上就会被中文行压住；表格/公式仍按 obstacles 挖洞。
                union = pymupdf.Rect(block_redact_rects[0])
                for line_rect in block_redact_rects[1:]:
                    union |= line_rect
                contract_obstacles = obstacles_by_block.get(id(block)) or []
                for band in plan_bands(
                    union, contract_obstacles, min_width_ratio=0.0, min_height=0.5
                ):
                    inflated = pymupdf.Rect(
                        band.x0 - 0.8, band.y0, band.x1 + 0.8, band.y1
                    )
                    for piece in _redact_bands_avoid_math(inflated, page_math_rects):
                        page.add_redact_annot(piece)
            else:
                for band in redact_bands_by_block[id(block)]:
                    # 横带下界按「下一块上沿」算，会切进紧邻的独立公式（第 7 页
                    # `D =` 分段函数上半截被擦掉）。横带只做左右外扩、上下不外扩，
                    # 再按公式矩形做 **纵向** 切分：公式占的那几 pt 不擦，其余全宽
                    # 照擦。不能按 x 切——公式多为整行宽，会把整条横带判成重叠、
                    # 结果一点都没擦，整段英文留在页面上。
                    inflated = pymupdf.Rect(
                        band.x0 - 0.8, band.y0, band.x1 + 0.8, band.y1
                    )
                    for piece in _redact_bands_avoid_math(inflated, page_math_rects):
                        page.add_redact_annot(piece)
                if not redact_bands_by_block[id(block)] and not block.get("inline_math"):
                    page.add_redact_annot(_inflate(rect))
            # 横带在公式附近可能切得不够细，逐词补擦非公式文字，
            # 否则会留下 Figure 1:/Given a... 这类英文碎片。
            obstacles = obstacles_by_block.get(id(block)) or []

            if obstacles and not block_redact_rects:
                for word in page.get_text("words", clip=pymupdf.Rect(rect)):
                    for piece in _redact_outside(pymupdf.Rect(word[:4]), obstacles):
                        page.add_redact_annot(piece)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            # 矢量线必须保留：redact 横带与表格横线/页眉分隔线相交是常态
            # （实测第 1 页 7 条表线被整条删除）。行内公式的原字形改用
            # 下方白块覆盖处理，不再靠 redact 删线。
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )
        # Type3/CID 公式字形 redact 擦不掉，用白色块盖住原公式区域；
        # 随后写入矢量 LaTeX（失败则内联裁图），页面上只看到一次公式。
        # 注意：白块 = 「我要擦掉原公式并重写」。只有确定会走 token 内联
        # （单横带 + 现场裁图齐全 + 每个公式可写回）时才盖；否则原公式会
        # 变成一块空白——第 7 页 `D =` 下方整段公式消失就是这个形态：
        # 含 25 个行内公式的段落横带被切成多条、不走 token 流，但白块照盖。
        for block, rect in targets:
            if id(block) in preserved_ids:
                continue
            math_items = block.get("inline_math") or []
            if not math_items:
                continue
            pixmaps = inline_pixmaps.get(id(block)) or {}
            block_bands = bands_by_block.get(id(block)) or []
            if not pixmaps or len(block_bands) != 1:
                continue
            rewritable = True
            for item in math_items:
                bbox = item.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                key = tuple(round(float(v), 1) for v in bbox)
                if key in pixmaps:
                    continue
                if latex_pdf(str(item.get("latex") or ""), size):
                    continue
                rewritable = False
                break
            if not rewritable:
                continue
            for item in math_items:
                bbox = item.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                page.draw_rect(
                    pymupdf.Rect(bbox),
                    color=(1, 1, 1),
                    fill=(1, 1, 1),
                    width=0,
                    overlay=True,
                )
        # 契约模式：未识别的 Type3 公式符号（×、=、+ 等）redact 擦不掉，
        # 用白块盖掉 union 内除 obstacles / 本块公式以外的区域，避免中文压符号。
        for block, rect in targets:
            if id(block) in preserved_ids:
                continue
            contract_rects = text_rects_by_block.get(id(block)) or []
            if not contract_rects:
                continue
            union = pymupdf.Rect(contract_rects[0])
            for line_rect in contract_rects[1:]:
                union |= line_rect
            own_math = [
                pymupdf.Rect(item["bbox"])
                for item in (block.get("inline_math") or [])
                if item.get("bbox") and len(item["bbox"]) >= 4
            ]
            cover_obstacles = list(obstacles_by_block.get(id(block)) or []) + own_math
            for band in plan_bands(
                union, cover_obstacles, min_width_ratio=0.0, min_height=0.5
            ):
                page.draw_rect(
                    band,
                    color=(1, 1, 1),
                    fill=(1, 1, 1),
                    width=0,
                    overlay=True,
                )
        # 每页只注册一次字体：每行都带 fontfile 会把整套字体重复嵌入，
        # 单页能从几百 KB 膨胀到近 10MB。
        page.insert_font(fontname="zhfs", fontfile=str(font_file))
        page.insert_font(fontname="zhhei", fontfile=str(heading_file))

    for block, rect in targets:
        if not dry_run and id(block) in preserved_ids:
            preserved_role = role_by_block.get(id(block), "body")
            preserved_size = (
                sidebar_size if preserved_role == "sidebar" else body_size
            )
            layout = BlockLayout(
                block_id=str(block.get("id")),
                kind=str(block.get("kind")),
                source_size=round(preserved_size, 2),
                font_size=round(preserved_size, 2),
                lines=0,
                needed=0.0,
                available=round(layout_rects[id(block)].height, 2),
                overflow=True,
                note="译文放不下，整块保留原文",
                role=preserved_role,
            )
            result.layouts.append(layout)
            continue
        page_text = _strip_heading_echo(
            _slice_translation(block, page_index), page_blocks, block
        )
        text = clean_translation(
                strip_block_leading_title(page_text, block.get("text")),
                block.get("inline_math"),
            )
        kind = block.get("kind")
        block_role = role_by_block.get(id(block), "body")
        if block_role == "sidebar":
            size, fontname, use_font = sidebar_size, "zhfs", font
            block_min = SIDEBAR_MIN_SIZE
        elif kind == "title":
            size, fontname, use_font = (
                round(body_size * TITLE_RATIO, 2),
                "zhhei",
                heading_font,
            )
            block_min = min_size
        elif kind == "heading":
            level = int(block.get("level") or 1)
            size = round(body_size * HEADING_RATIOS.get(level, 1.12), 2)
            # 标题字号不得超过自身 bbox 高度，否则字形溢出压到相邻行。
            size = min(
                size,
                max(
                    min_size,
                    (rect.height - 0.5)
                    / max(heading_font.ascender - heading_font.descender, 1.0),
                ),
            )
            fontname, use_font = "zhhei", heading_font
            block_min = min_size
        else:
            size, fontname, use_font = (
                _figure_text_size(page, block, rect, figure_text_ids, body_size),
                "zhfs",
                font,
            )
            block_min = (
                HARD_MIN_SIZE
                if id(block) in figure_text_ids
                else BODY_FLOOR_SIZE
            )
        bands = bands_by_block[id(block)]
        layout_rect = layout_rects[id(block)]

        # 单横带 + 有行内公式：走 token 流混排，公式裁图回到句子里。
        # 多横带（表格/公式绕排）暂时沿用纯文本排版。
        if (
            not dry_run
            and len(bands) == 1
            and inline_pixmaps.get(id(block))
            and _inline_math_ok(block, size)
        ):
            tokens = _tokenize_inline(
                page_text,
                block.get("inline_math") or [],
            )
            fit_size, fit_lh, inline_lines, inline_overflow = _layout_inline(
                use_font,
                tokens,
                bands[0],
                size,
                inline_pixmaps[id(block)],
                min_size=block_min,
                line_height=line_height,
            )
            layout = BlockLayout(
                block_id=str(block.get("id")),
                kind=str(block.get("kind")),
                source_size=round(size, 2),
                font_size=round(fit_size, 2),
                lines=sum(len(line) for line in inline_lines),
                needed=round(bands[0].height, 2),
                available=round(bands[0].height, 2),
                overflow=inline_overflow,
                line_height=round(fit_lh, 3),
                role=block_role,
            )
            band = bands[0]
            ascent = use_font.ascender * fit_size
            step = fit_size * fit_lh
            written = 0
            for line_index, line in enumerate(inline_lines):
                baseline = band.y0 + ascent + line_index * step
                if baseline + abs(use_font.descender) * fit_size > band.y1 + 0.5:
                    break
                x = band.x0 + 0.5
                run = ""
                run_start = x
                for atom in line:
                    if atom["type"] == "text":
                        if not run:
                            run_start = x
                        run += atom["text"]
                        x += atom["w"]
                        continue
                    if run:
                        page.insert_text(
                            (run_start, baseline),
                            run,
                            fontname=fontname,
                            fontsize=fit_size,
                            color=(0, 0, 0),
                        )
                        run = ""
                    x += 1.2
                    center_y = baseline - fit_size * 0.30
                    image_rect = pymupdf.Rect(
                        x,
                        center_y - atom["h"] / 2,
                        x + atom["w"],
                        center_y + atom["h"] / 2,
                    )
                    rendered = False
                    if atom.get("latex"):
                        rendered = insert_latex(
                            page, image_rect, atom["latex"], fit_size
                        )
                    pixmap = atom.get("pixmap")
                    if not rendered and pixmap is not None:
                        page.insert_image(image_rect, pixmap=pixmap)
                        rendered = True
                    if not rendered and str(atom.get("source") or "").strip():
                        page.insert_text(
                            (x, baseline),
                            str(atom["source"]).strip(),
                            fontname=fontname,
                            fontsize=fit_size * 0.9,
                            color=(0, 0, 0),
                        )
                    x += atom["w"] + 1.2
                if run:
                    page.insert_text(
                        (run_start, baseline),
                        run,
                        fontname=fontname,
                        fontsize=fit_size,
                        color=(0, 0, 0),
                    )
                written += 1
            layout.lines = written
            if inline_overflow:
                layout.note = "行内公式排版超出横带"
            result.layouts.append(layout)
            continue
        fit_size, fit_lh, placements, remaining = layout_in_rows(
            use_font,
            text,
            layout_rect,
            obstacles,
            size,
            min_size=block_min,
            max_size=max(max_size, size),
            line_height=line_height,
        )
        full_lines = wrap_text(use_font, text, max(rect.width - 1, 8), fit_size)
        needed = (use_font.ascender - use_font.descender) * fit_size + max(
            0, len(full_lines) - 1
        ) * fit_size * fit_lh
        # 页内正文统一字号、统一行距：不再按块拉伸行距填白，块的富余空间
        # 交给「后面的块上移」处理，保证整页正文格式一致。
        layout = BlockLayout(
            block_id=str(block.get("id")),
            kind=str(block.get("kind")),
            source_size=round(size, 2),
            font_size=round(fit_size, 2),
            lines=sum(len(lines) for _band, lines in placements),
            needed=round(needed, 2),
            available=round(max(layout_rect.height, 1.0), 2),
            overflow=bool(remaining.strip()),
            line_height=round(fit_lh, 3),
            role=block_role,
        )

        if not dry_run:
            written = 0
            for band, lines in placements:
                ascent = use_font.ascender * fit_size
                step = fit_size * fit_lh
                for idx, line in enumerate(lines):
                    baseline = band.y0 + ascent + idx * step
                    if baseline + abs(use_font.descender) * fit_size > band.y1 + 0.5:
                        break
                    line_width = use_font.text_length(line, fit_size)
                    needs_justify = (
                        idx < len(lines) - 1
                        and band.width > line_width + 3
                        and any("\u4e00" <= ch <= "\u9fff" for ch in line)
                    )
                    if needs_justify:
                        _write_justified_line(
                            page,
                            band.x0 + 0.5,
                            baseline,
                            line,
                            use_font,
                            fontname,
                            fit_size,
                            band.width - 1.0,
                        )
                    else:
                        page.insert_text(
                            (band.x0 + 0.5, baseline),
                            line,
                            fontname=fontname,
                            fontsize=fit_size,
                            color=(0, 0, 0),
                        )
                    written += 1
            layout.lines = written
            if layout.overflow:
                layout.note = "横带容量不足，尾部裁切"
        result.layouts.append(layout)

    if not dry_run:
        # 原页 highlight/underline 会被 insert_pdf 原样带进译文页，盖在中文上；
        # 保存前显式删除（链接也一并删除，避免错位）。
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
        result.restored_keep_blocks = 0
        result.images_after = len(page.get_images(full=True))
        result.drawings_after = len(page.get_drawings())
        result.fonts = page.get_fonts()
        residue: list[str] = []
        for block, rect in targets:
            src = " ".join((block.get("text") or "").split())
            if len(src) < 8:
                continue
            probe = src[:24]
            try:
                hits = page.search_for(probe, clip=pymupdf.Rect(rect))
            except Exception:
                hits = []
            if hits:
                # 行内公式是原样保留的，命中落在公式区域不算残留
                formula_boxes = [
                    pymupdf.Rect(item["bbox"])
                    for item in (block.get("inline_math") or [])
                    if item.get("bbox")
                ]
                outside = [
                    h for h in hits
                    if not any(not (h & fb).is_empty for fb in formula_boxes)
                ]
                if outside:
                    residue.append(str(block.get("id")))
        result.residue_blocks = residue
        result.original_left = bool(residue)

    if not dry_run and out_pdf:
        out_path = Path(out_pdf)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = pymupdf.open()
        tmp.insert_pdf(doc, from_page=page_index, to_page=page_index)
        try:
            tmp.subset_fonts()
        except Exception:
            pass
        tmp.save(str(out_path), garbage=3, deflate=True)
        tmp.close()

    doc.close()
    return result


def _page_hash(
    blocks: list[dict],
    page_index: int,
    *,
    lang: str,
    size: float,
    sidebar_size: float = 0.0,
    font_path: Path | str | None,
    heading_font_path: Path | str | None,
) -> str:
    """只对影响该页渲染的字段取哈希；某页没变就复用旧 p{n}.pdf。"""
    rows: list[dict] = []
    for block in blocks:
        if _rect(block, page_index) is None:
            continue
        rows.append(
            {
                key: block.get(key)
                for key in (
                    "id",
                    "kind",
                    "text",
                    "translated",
                    "bbox",
                    "bboxes",
                    "translatable",
                    "caption",
                    "latex",
                    "eq_number",
                    "table_html",
                    "inline_math",
                    "text_rects",
                )
            }
        )
    raw = json.dumps(
        {
            "page": page_index + 1,
            "lang": lang,
            "size": round(float(size), 3),
            "sidebar_size": round(float(sidebar_size), 3),
            "font": str(font_path or ""),
            "heading_font": str(heading_font_path or ""),
            "algo": PDF_ALGO_VER,
            "block_strict": env_block_strict(),
            "blocks": rows,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _count_overlap(page: pymupdf.Page) -> int:
    """统计同页写在同一位置的两段文字（不同行、横纵向都实质相交）。

    这是「任何两块不得共享同一行」的自动验收：构建后直接读回文本层，
    不依赖人工看图，也不对具体论文做任何特判。同一行内的多个 span 属于
    并排（中文与公式混排），不参与比较。
    """
    lines: list[list[pymupdf.Rect]] = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            rects = [
                pymupdf.Rect(span["bbox"])
                for span in line["spans"]
                # 只比较写入的中文：原文保留的表格 ID/英文串本身就密集并排，
                # 算进来会成批误报（实测一篇 297 条全是英文 ID）。
                if span["text"].strip()
                and any("\u4e00" <= ch <= "\u9fff" for ch in span["text"])
            ]
            if rects:
                lines.append(rects)
    count = 0
    for index, left_line in enumerate(lines):
        for right_line in lines[index + 1:]:
            hit = False
            for left in left_line:
                for right in right_line:
                    inter = left & right
                    if (
                        inter.is_valid
                        and not inter.is_empty
                        and inter.width > min(left.width, right.width) * 0.3
                        and inter.height > min(left.height, right.height) * 0.3
                    ):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                count += 1
    return count


def _dominant_column(
    words: list[tuple], band: float = 16.0, gap_bins: int = 1
) -> tuple[float, float] | None:
    """从 words 的横向分布估出「主体文字栏」的 x 区间。

    跨栏块（如左窄栏 + 右正文被解析成一块）的 bbox 覆盖整页，但 words 的
    绝大多数落在其中一个栏里；按分箱聚类取词数最多的连续区间，避免译文
    横跨两栏、压到作者单位侧栏上。
    """
    if not words:
        return None
    bins: dict[int, int] = {}
    for word in words:
        center = (float(word[0]) + float(word[2])) / 2
        key = int(center // band)
        bins[key] = bins.get(key, 0) + 1
    keys = sorted(bins)
    best: list[int] = []
    current = [keys[0]]
    for key in keys[1:]:
        if key - current[-1] <= gap_bins:
            current.append(key)
        else:
            if sum(bins[k] for k in current) > sum(bins[k] for k in best):
                best = current
            current = [key]
    if sum(bins[k] for k in current) > sum(bins[k] for k in best):
        best = current
    if not best:
        return None
    return (min(best) * band, (max(best) + 1) * band)


def _looks_like_prose(block: dict) -> bool:
    """成段正文（够长且带句末标点）不得被当作信息侧栏。

    侧栏内容是单位/城市/资助声明这类短语，逐行排列；正文列哪怕很窄，
    只要出现成段句子就说明它是正文，宁可当正文排也不收窄。
    """
    text = " ".join((block.get("text") or "").split())
    if len(text) < SIDEBAR_PROSE_CHARS:
        return False
    stripped = text.rstrip()
    if stripped[-1:] in "。！？.!?":
        return True
    return sum(stripped.count(ch) for ch in "。！？") >= 2 or stripped.count(". ") >= 2


def _sidebar_cluster_rows(
    pairs: list[tuple[dict, pymupdf.Rect]],
) -> list[tuple[dict, pymupdf.Rect]]:
    """把同一 x0 的窄块聚成簇，取纵向连续的最长链（逐行排列）。"""
    # 信息栏多为居中短行，左缘随文本长度变化；用右缘对齐聚类才稳定。
    pairs = sorted(pairs, key=lambda item: item[1].x1)
    clusters: list[list[tuple[dict, pymupdf.Rect]]] = []
    for block, rect in pairs:
        if clusters and abs(rect.x1 - clusters[-1][-1][1].x1) <= SIDEBAR_X1_SPREAD:
            clusters[-1].append((block, rect))
        else:
            clusters.append([(block, rect)])
    best: list[tuple[dict, pymupdf.Rect]] = []
    for cluster in clusters:
        cluster.sort(key=lambda item: item[1].y0)
        chain: list[tuple[dict, pymupdf.Rect]] = []
        for item in cluster:
            if chain and item[1].y0 - chain[-1][1].y0 > SIDEBAR_Y_GAP:
                if len(chain) > len(best):
                    best = chain
                chain = []
            chain.append(item)
        if len(chain) > len(best):
            best = chain
    return best


def _looks_like_person_name(text: str) -> bool:
    """人名/作者行默认保留原样：音译易错，且侧栏窄列放不下中文。

    方案白名单项（人名默认不译，``translate_person_names`` 默认关）。
    只认「1–4 个首字母大写的词、无数字/邮箱/URL、无 of/for/in 等虚词」，
    机构名与期刊名不在此列，仍按可译实体处理。
    """
    cleaned = " ".join((text or "").split()).strip(" ,;")
    if not cleaned or len(cleaned) > 40:
        return False
    if any(ch.isdigit() for ch in cleaned) or "@" in cleaned:
        return False
    if "http" in cleaned.lower() or "doi" in cleaned.lower():
        return False
    words = cleaned.replace(",", " ").split()
    if not 1 <= len(words) <= 4:
        return False
    particles = {
        "of", "for", "and", "the", "in", "at", "de", "van", "von", "der", "&",
    }
    if any(word.lower() in particles for word in words):
        return False
    # 机构 / 地名不是人名：带机构后缀或国家/州缩写（全大写且长度 > 2）。
    org_words = {
        "hospital", "university", "institute", "college", "laboratory",
        "department", "center", "centre", "school", "foundation", "inc",
        "ltd", "gmbh", "clinic", "academy", "society", "association",
        "committee", "group", "press", "journal", "research", "science",
        "sciences",
    }
    if any(word.lower().strip(",.") in org_words for word in words):
        return False
    if any(word.isupper() and len(word) > 2 for word in words):
        return False
    return all(word[:1].isupper() for word in words if word)


def _detect_sidebar_bands(
    page_blocks: list[tuple[dict, pymupdf.Rect]],
    page_width: float,
    keep_rects: list[pymupdf.Rect] | None = None,
) -> list[pymupdf.Rect]:
    """识别页缘窄信息栏（作者单位 / 期刊信息 / 资助与利益声明）的区间。

    通用几何判据，不按单篇特判：
      * 列宽 < 0.22 页宽，且各行的右缘对齐（同一窄列）；
      * 列中心落在页缘 0–32% 或 68–100%；
      * 列内至少 3 行，纵向连续排列（相邻行间距 <= 40pt）；
      * 页面上必须同时存在明显更宽（>= 0.36 页宽）的正文列；
      * 与主正文列之间至少 6pt 横向空白沟。
    否决条件：
      * 列内出现 >=120 字符且带句末标点的成段正文 → 正文列；
      * 多半落在图 / 表 / 公式区 → 图内标注、表格列，不是信息栏；
      * 列内文本高度重复（同一句写很多遍）→ 表格/图例行。
    """
    if not page_blocks:
        return []
    keep_rects = keep_rects or []
    bands: list[pymupdf.Rect] = []
    for side in ("left", "right"):
        edge = [
            (block, rect)
            for block, rect in page_blocks
            if (block.get("kind") or "") in SIDEBAR_ROW_KINDS
            and (
                ((rect.x0 + rect.x1) / 2 < page_width * SIDEBAR_EDGE_RATIO)
                if side == "left"
                else ((rect.x0 + rect.x1) / 2 > page_width * (1 - SIDEBAR_EDGE_RATIO))
            )
            # 图/表/公式区里的文字是标注或表格元素，不是信息栏。
            and not any(
                not (rect & other).is_empty
                and (rect & other).get_area() > rect.get_area() * 0.5
                for other in keep_rects
            )
        ]
        group = _sidebar_cluster_rows(edge)
        if len(group) < SIDEBAR_MIN_BLOCKS:
            continue
        x0 = min(rect.x0 for _block, rect in group)
        x1 = max(rect.x1 for _block, rect in group)
        if x1 - x0 > page_width * SIDEBAR_RATIO_MAX:
            continue
        if x1 - x0 < page_width * SIDEBAR_RATIO_MIN:
            continue
        if max(rect.x1 for _block, rect in group) - min(
            rect.x1 for _block, rect in group
        ) > SIDEBAR_X1_SPREAD:
            continue
        rows = sum(
            1 for _block, rect in group if rect.height <= SIDEBAR_LINE_MAX_HEIGHT
        )
        if rows < SIDEBAR_MIN_BLOCKS:
            continue
        if any(_looks_like_prose(block) for block, _rect in group):
            continue
        texts = [
            " ".join((block.get("text") or "").split())
            for block, _rect in group
            if (block.get("text") or "").strip()
        ]
        if texts:
            most = max(texts.count(value) for value in set(texts))
            if most >= max(2, len(texts) // 2):
                continue
        # 侧栏是「一整列」：band 的 y 范围要覆盖该列所有行，而不是只看
        # 最长连续链。否则同列上下相隔较远的行会被当成正文写进侧栏列。
        col_items = [
            item
            for item in edge
            if abs(item[1].x1 - group[0][1].x1) <= SIDEBAR_X1_SPREAD
        ]
        group_ids = {id(block) for block, _rect in group}
        rest = [
            (block, rect)
            for block, rect in page_blocks
            if id(block) not in group_ids
            and (block.get("kind") or "") in BODY_KINDS
        ]
        if not rest:
            continue
        if not any(rect.width >= page_width * SIDEBAR_BODY_MIN_RATIO for _b, rect in rest):
            continue
        if side == "left":
            right_edges = [rect.x0 for _b, rect in rest if rect.x0 >= x1 - 2]
            if not right_edges or min(right_edges) - x1 < SIDEBAR_GUTTER:
                continue
            band = pymupdf.Rect(
                0.0,
                min(rect.y0 for _block, rect in col_items),
                x1 + 0.5,
                max(rect.y1 for _block, rect in col_items),
            )
        else:
            left_edges = [rect.x1 for _b, rect in rest if rect.x1 <= x0 + 2]
            if not left_edges or x0 - max(left_edges) < SIDEBAR_GUTTER:
                continue
            band = pymupdf.Rect(
                x0 - 0.5,
                min(rect.y0 for _block, rect in col_items),
                page_width,
                max(rect.y1 for _block, rect in col_items),
            )
        bands.append(band)
    return bands


def _in_sidebar(rect: pymupdf.Rect, bands: list[pymupdf.Rect]) -> pymupdf.Rect | None:
    """块整体落在某个侧栏列内时返回该列。

    侧栏是「整列」：横向区间是硬边界，纵向不设限（块可以在本列内向下借用
    空白，或与列内其他行错开纵向位置）。
    """
    for band in bands:
        if rect.x0 >= band.x0 - 2.5 and rect.x1 <= band.x1 + 2.5:
            return band
    return None


def _probe_page_numbers(blocks: list[dict], page_count: int) -> set[int]:
    """字号试探的采样页号（1-indexed）。

    大论文每档字号都全篇渲染会让构建远超 HTTP 超时（35 页实测 15 分钟）。
    采样页覆盖「首页 / 文本最多页 / 中间页 / 末页」，足以判断该档是否让任何
    块被压缩、溢出或重叠；采用新字号后仍全篇重排一次保证版式统一。
    """
    if page_count <= 6:
        return set(range(1, page_count + 1))
    weights: dict[int, int] = {}
    for block in blocks:
        page = int(block.get("page") or 0)
        text = (block.get("text") or "").strip()
        if 1 <= page <= page_count and text:
            weights[page] = weights.get(page, 0) + len(text)
    heaviest = max(weights, key=weights.get) if weights else 1
    picks = {1, heaviest, page_count}
    return {p for p in picks if 1 <= p <= page_count}


def _copy_original_page(pdf_path: Path | str, page_index: int, out_pdf: Path) -> None:
    """单页渲染失败时的兜底：原页原样写成一页 PDF，保证页数与尺寸不变。"""
    src = pymupdf.open(pdf_path)
    try:
        dst = pymupdf.open()
        try:
            dst.insert_pdf(src, from_page=page_index, to_page=page_index)
            dst.save(str(out_pdf))
        finally:
            dst.close()
    finally:
        src.close()


def _commit_pdf(src: Path, dst: Path, attempts: int = 10) -> Path:
    """把译文 PDF 原子替换到位。

    Windows 上目标文件可能正被 pdf.js / 下载请求读取，``os.replace`` 会短暂
    报 WinError 32；重试后仍失败就落到 ``translated.next.pdf``，读取方取较新
    的那一份，避免整个构建因为一次占用而失败。
    """
    for index in range(attempts):
        try:
            os.replace(src, dst)
            _prune_old_copies(dst)
            return dst
        except OSError:
            if index < attempts - 1:
                time.sleep(0.3)
    next_path = dst.with_name("translated.next.pdf")
    try:
        os.replace(src, next_path)
        return next_path
    except OSError:
        pass
    # 正式名与 next 都被占用：写唯一文件名，读取方按修改时间取最新一份。
    unique = dst.with_name(
        f"translated.{int(time.time() * 1000)}.{uuid.uuid4().hex[:6]}.pdf"
    )
    os.replace(src, unique)
    return unique


def _prune_old_copies(dst: Path) -> None:
    """正式文件替换成功后，清掉此前的 next/唯一名副本（被占用时忽略）。"""
    for old in dst.parent.glob("translated*.pdf"):
        if old == dst:
            continue
        try:
            old.unlink()
        except OSError:
            pass


def _promote_translated(out: Path) -> None:
    """把上次因占用写到 next/唯一名的产物提升为 translated.pdf。

    占用解除后（包括整篇缓存命中的快速路径）都要收口，避免目录里长期
    留着多份副本；提升失败则保持原样，由读取方按修改时间取最新。
    """
    main = out / "translated.pdf"
    try:
        copies = [p for p in out.glob("translated*.pdf") if p.is_file()]
    except OSError:
        return
    if not copies:
        return
    latest = max(copies, key=lambda p: p.stat().st_mtime)
    if latest != main:
        try:
            os.replace(latest, main)
        except OSError:
            return
    _prune_old_copies(main)


def _page_detail(page_pdf: Path, res: Any) -> dict[str, Any]:
    doc = pymupdf.open(page_pdf)
    page = doc[0]
    text = page.get_text()
    overlap = _count_overlap(page)
    doc.close()
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    body_layouts = [x for x in res.layouts if x.role != "sidebar"]
    side_layouts = [x for x in res.layouts if x.role == "sidebar"]
    return {
        "page": res.page + 1,
        "blocks": len(res.layouts),
        "body_blocks": len(body_layouts),
        "sidebar_blocks": len(side_layouts),
        "overflow": [x.block_id for x in body_layouts if x.overflow],
        "sidebar_overflow": [x.block_id for x in side_layouts if x.overflow],
        "residue": res.residue_blocks,
        "skipped": res.skipped,
        "overlap": overlap,
        "cjk": cjk,
        "cross_leaks": list(res.cross_leaks),
        "sidebar_bands": [list(x) for x in res.sidebar_bands],
    }



def env_in_place_strict() -> bool:
    """``PAPEREAD_INPLACE_STRICT=0``：关闭块级原位约束（回退旧的合并/借位行为）。"""
    return _env_flag("PAPEREAD_INPLACE_STRICT")


def env_block_strict() -> bool:
    """``PAPEREAD_BLOCK_STRICT=1``：启用 text_rects 块几何契约（实验）。

    新解析产物带 ``text_rects`` 时，擦写范围只取实际文字行，不再用跨栏
    收窄 / 短标签扩栏补偿。默认关闭：当前解析仍有块范围重叠与 Type3
    公式字形残留，契约模式会把这些暴露成可见叠字，等解析层去重后再默认开。
    """
    return _env_flag("PAPEREAD_BLOCK_STRICT", False)


def build_translated_pdf(
    pdf_path: Path | str,
    blocks: list[dict],
    out_dir: Path | str,
    *,
    lang: str = "zh-Hans",
    font_path: Path | str | None = None,
    heading_font_path: Path | str | None = None,
    body_size: float = AUTO_BODY_SIZE,
    line_height: float = DEFAULT_LINE_HEIGHT,
    min_size: float = DEFAULT_MIN_SIZE,
    max_size: float = DEFAULT_MAX_SIZE,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """构建整篇译文 PDF；进程内串行，避免并发请求争抢同一目录的文件。

    Windows 上两个请求同时写 ``p{n}.pdf`` / ``translated.pdf`` 会抛
    ``PermissionError(WinError 32)``，对用户表现为「译文 PDF 生成失败（HTTP 500）」。
    """
    with _BUILD_LOCK:
        return _build_translated_pdf(
            pdf_path,
            blocks,
            out_dir,
            lang=lang,
            font_path=font_path,
            heading_font_path=heading_font_path,
            body_size=body_size,
            line_height=line_height,
            min_size=min_size,
            max_size=max_size,
            cache_key=cache_key,
        )


def _build_translated_pdf(
    pdf_path: Path | str,
    blocks: list[dict],
    out_dir: Path | str,
    *,
    lang: str = "zh-Hans",
    font_path: Path | str | None = None,
    heading_font_path: Path | str | None = None,
    body_size: float = AUTO_BODY_SIZE,
    line_height: float = DEFAULT_LINE_HEIGHT,
    min_size: float = DEFAULT_MIN_SIZE,
    max_size: float = DEFAULT_MAX_SIZE,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """逐页回填并合并为整篇译文 PDF。

    必须逐页 ``insert_pdf`` 合并：多页 ``doc.save()`` 会丢掉之后写入的
    overlay 文字（中文会整篇消失），这是 P1 已确认的硬约束。

    增量策略：每页按「原文 + 译文 + 字号 + 算法版本」取哈希，命中时直接复用
    ``p{n}.pdf``；只有译文变化的页才重新渲染。字号仍按全篇统一：若变化页
    需要更小字号，则回退到全量重排。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # 上次异常中断可能留下试探目录，先清掉，避免堆积与文件句柄冲突。
    for stale in out.glob("_probe*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    # 上次若因占用写到 next/唯一名，这里收口回 translated.pdf。
    _promote_translated(out)

    meta_path = out / "meta.json"
    cached_meta: dict[str, Any] = {}
    try:
        cached_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        cached_meta = {}

    # 整篇 blocks 完全没变：直接返回上一次产物。
    if cache_key:
        if (
            cached_meta.get("cache_key") == cache_key
            and cached_meta.get("algo_ver") == PDF_ALGO_VER
            and cached_meta.get("lang") == lang
            and (out / "translated.pdf").is_file()
        ):
            return {
                **cached_meta,
                "cached": True,
                "pages_reused": int(cached_meta.get("pages") or 0),
                "pages_rendered": 0,
            }

    src = pymupdf.open(pdf_path)
    page_count = src.page_count
    sizes = [
        (round(src[i].rect.width, 1), round(src[i].rect.height, 1))
        for i in range(page_count)
    ]
    src.close()

    pages_meta_path = out / "pages.meta.json"
    try:
        page_cache = json.loads(pages_meta_path.read_text(encoding="utf-8"))
    except Exception:
        page_cache = {}
    if (
        page_cache.get("algo_ver") != PDF_ALGO_VER
        or page_cache.get("lang") != lang
    ):
        page_cache = {}
    cached_pages: dict[str, Any] = dict(page_cache.get("pages") or {})

    requested = max(6.5, min(body_size, 13.0))
    known_size = cached_meta.get("body_size_used") if cached_pages else None
    try:
        known_size = float(known_size) if known_size else None
    except (TypeError, ValueError):
        known_size = None
    if known_size:
        known_size = max(6.5, min(known_size, 13.0))


    # 页级字号自适应：同一页内正文只有一个字号、一个行距，页与页之间可以不同。
    # 判据是「该页所有块在统一行距下不压缩、不溢出」——避免像全篇统一字号那样
    # 被最长段落拖小，也让内容稀疏的页自动放大字号、减少大片空白。
    page_sizes: dict[int, float] = {}
    page_sidebar_sizes: dict[int, float] = {}
    for key, slot in cached_pages.items():
        try:
            page_no = int(key)
            value = float(slot.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= page_no <= page_count and value >= BODY_HARD_MIN_SIZE:
            page_sizes[page_no] = value
            try:
                side_value = float(slot.get("sidebar_size") or 0)
            except (TypeError, ValueError):
                side_value = 0.0
            if side_value >= SIDEBAR_MIN_SIZE:
                page_sidebar_sizes[page_no] = side_value

    # 字号锚点：逐页读原文正文主字号，中文基准按 CJK_SIZE_RATIO 折算。
    # 字号不再由「页面拥挤度」选出绝对档位，而是跟着原文正文走，避免
    # 同一篇论文 p4=7.6、p1=10.5 这种翻页字号跳变。
    page_anchors: dict[int, float] = {}
    try:
        anchor_doc = pymupdf.open(pdf_path)
        try:
            for anchor_index in range(anchor_doc.page_count):
                page_anchors[anchor_index + 1] = _page_body_source_size(
                    anchor_doc[anchor_index]
                )
        finally:
            anchor_doc.close()
    except Exception as exc:
        log.warning("读取页码字号锚点失败：%s", exc)

    sidebar_seen: dict[int, bool] = {}
    for index in range(page_count):
        page_no = index + 1
        if page_no in page_sizes:
            continue
        chosen: float | None = None
        candidates = _body_size_candidates(
            page_anchors.get(page_no, BODY_FONT_SIZE)
        )

        def _bad(step: float) -> int | None:
            try:
                res = render_page(
                    pdf_path,
                    blocks,
                    index,
                    None,
                    body_size=step,
                    line_height=line_height,
                    min_size=min_size,
                    max_size=max(max_size, step),
                    dry_run=True,
                )
            except Exception as exc:
                log.warning("页 %d 字号探测失败：%s", page_no, exc)
                return None
            if not res.layouts:
                return 0
            # 侧栏块有自己的字号档位，不能把正文页级字号往下拖。
            # 只把「整块放不下（preserved）」算坏：块内为极端长段压到 6–7pt
            # 属局部兜底，不能反过来把整页字号拖小。
            return sum(
                1
                for layout in res.layouts
                if layout.role != "sidebar" and layout.overflow
            )

        # 「越大越难」是单调的：被压缩/溢出的块数随字号上升不下降，二分
        # 3–4 次即可。候选围绕原文正文锚点生成，不再跨到 12pt 或 7.6pt。
        best_step: float | None = None
        best_bad = 1 << 30
        left, right = 0, len(candidates) - 1
        while left <= right:
            mid = (left + right) // 2
            step = candidates[mid]
            bad = _bad(step)
            if bad is None:
                break
            if bad == 0:
                chosen = step
                right = mid - 1
            else:
                # 个别块物理放不下时，取「被压缩块最少」的最大档位，而不是
                # 让整页跟着降到最小字号（那会把整页字压得过小）。
                if bad < best_bad:
                    best_step, best_bad = step, bad
                left = mid + 1
        page_sizes[page_no] = max(
            BODY_HARD_MIN_SIZE, float(chosen or best_step or min_size)
        )

        # 侧栏列级字号：与正文页级字号互不影响。无侧栏的页只多探测一次。
        if not env_sidebar_translate():
            page_sidebar_sizes[page_no] = SIDEBAR_MIN_SIZE
            continue
        chosen_side: float | None = None
        best_side: float | None = None
        best_side_bad = 1 << 30

        def _bad_side(step: float) -> int | None:
            if sidebar_seen.get(page_no) is False:
                return 0
            try:
                res = render_page(
                    pdf_path,
                    blocks,
                    index,
                    None,
                    body_size=page_sizes[page_no],
                    line_height=line_height,
                    min_size=min_size,
                    max_size=max(max_size, requested),
                    sidebar_size=step,
                    dry_run=True,
                )
            except Exception as exc:
                log.warning("页 %d 侧栏字号探测失败：%s", page_no, exc)
                return None
            side_layouts = [item for item in res.layouts if item.role == "sidebar"]
            sidebar_seen[page_no] = bool(side_layouts) or res.sidebar_blocks > 0
            if not side_layouts:
                return 0
            return sum(
                1
                for item in side_layouts
                if item.overflow or item.font_size < step - 0.01
            )

        # 侧栏列级字号：从高到低取第一个「至多 1 块放不下」的档位。侧栏块
        # 少、长度差异大，不能用「全放得下」当唯一判据，否则一个长行（中文
        # 比英文长 2–3 倍）会把整列压到 6.5pt；放不下的那一块按三态
        # preserved 记账，不牺牲整列可读性。
        chosen_side: float | None = None
        best_side: float | None = None
        best_side_bad = 1 << 30
        for step in SIDEBAR_SIZE_STEPS:
            bad = _bad_side(step)
            if bad is None or sidebar_seen.get(page_no) is False:
                break
            if bad <= 1:
                chosen_side = step
                break
            if bad < best_side_bad:
                best_side, best_side_bad = step, bad
        if sidebar_seen.get(page_no) is False:
            page_sidebar_sizes[page_no] = min(
                page_sizes[page_no], SIDEBAR_MAX_SIZE
            )
            continue
        page_sidebar_sizes[page_no] = max(
            SIDEBAR_MIN_SIZE, float(chosen_side or best_side or SIDEBAR_MIN_SIZE)
        )

    failed_pages: set[int] = set()

    def _render_one(
        index: int, target_dir: Path
    ) -> tuple[dict[str, Any], float, int, int]:
        size_value = page_sizes[index + 1]
        side_value = page_sidebar_sizes.get(index + 1, SIDEBAR_MIN_SIZE)
        page_pdf = target_dir / f"p{index + 1}.pdf"
        digest = _page_hash(
            blocks,
            index,
            lang=lang,
            size=size_value,
            sidebar_size=side_value,
            font_path=font_path,
            heading_font_path=heading_font_path,
        )

        cached = cached_pages.get(str(index + 1)) or {}
        if (
            target_dir == out
            and cached.get("hash") == digest
            and abs(float(cached.get("size", -1)) - size_value) < 0.001
            and abs(float(cached.get("sidebar_size", -1)) - side_value) < 0.001
            and page_pdf.is_file()
            and cached.get("detail")
        ):
            cached_detail = cached["detail"]
            return (
                {
                    "page": index + 1,
                    "hash": digest,
                    "size": size_value,
                    "sidebar_size": side_value,
                    "reused": True,
                    "detail": cached_detail,
                },
                size_value,
                side_value,
                len(cached_detail.get("overflow") or []),
                int(cached_detail.get("overlap") or 0),
            )
        try:
            res = render_page(
                pdf_path,
                blocks,
                index,
                page_pdf,
                font_path=font_path,
                heading_font_path=heading_font_path,
                body_size=size_value,
                line_height=line_height,
                min_size=min_size,
                max_size=max_size,
                sidebar_size=side_value,
            )
            detail = _page_detail(page_pdf, res)
        except Exception as exc:
            # 单页失败（异常版面/字体/占用）不能让整篇构建失败：原页占位并记账。
            log.warning("译文页渲染失败 p%d：%s", index + 1, exc)
            try:
                _copy_original_page(pdf_path, index, page_pdf)
            except Exception:
                pass
            failed_pages.add(index + 1)
            detail = {
                "page": index + 1,
                "blocks": 0,
                "overflow": [],
                "residue": [],
                "skipped": ["render_error"],
                "overlap": 0,
                "cjk": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return (
                {
                    "page": index + 1,
                    "hash": digest,
                    "size": size_value,
                    "sidebar_size": side_value,
                    "reused": False,
                    "detail": detail,
                },
                size_value,
                side_value,
                0,
                0,
            )
        smallest = size_value
        overflow = 0
        for layout in res.layouts:
            if layout.kind in ("heading", "title") or layout.role == "sidebar":
                continue
            smallest = min(smallest, layout.font_size)
            if layout.overflow:
                overflow += 1
        return (
            {
                "page": index + 1,
                "hash": digest,
                "size": size_value,
                "sidebar_size": side_value,
                "reused": False,
                "detail": detail,
            },
            smallest,
            side_value,
            overflow,
            int(detail.get("overlap") or 0),
        )

    def _run(
        target_dir: Path, only: set[int] | None = None
    ) -> tuple[list[dict[str, Any]], float, float, int, int]:
        """渲染页（默认全篇），返回 (页信息, 最小正文字号, 最小侧栏字号, 溢出块数, 重叠对数)。"""
        indexes = [i for i in range(page_count) if only is None or (i + 1) in only]
        # PyMuPDF 渲染在进程内有全局锁，多线程实测不加速（12.5s vs 11.5s），
        # 因此保持串行，避免线程开销与字体加载竞争。
        rows = [_render_one(i, target_dir) for i in indexes]
        info = [row[0] for row in rows]
        smallest = min((row[1] for row in rows), default=min(page_sizes.values()))
        side_smallest = min(
            (row[2] for row in rows), default=SIDEBAR_MIN_SIZE
        )
        overflow_total = sum(row[3] for row in rows)
        overlap_total = sum(row[4] for row in rows)
        return info, smallest, side_smallest, overflow_total, overlap_total

    # 字号已在上面按页算好（页级自适应），这里直接按页级字号渲染一遍。
    pages_info, min_font, min_side_font, overflow_count, overlap_count = _run(out)
    # 报告口径用「页面正文字号的中位数」：min_font 会把图内标注缩到 5pt 的
    # 局部字号也带进来，显示成「整篇 5pt」，与实际观感不符。
    reported_sizes = sorted(page_sizes.values())
    size = reported_sizes[len(reported_sizes) // 2] if reported_sizes else min_font

    totals = {"blocks": 0, "overflow": 0, "residue": 0, "overlap": 0, "cjk": 0}
    detail = [slot["detail"] for slot in pages_info]
    for row in detail:
        totals["blocks"] += int(row.get("blocks") or 0)
        totals["overflow"] += len(row.get("overflow") or [])
        totals["residue"] += len(row.get("residue") or [])
        totals["overlap"] += int(row.get("overlap") or 0)
        totals["cjk"] += int(row.get("cjk") or 0)

    merged = pymupdf.open()
    for index in range(page_count):
        page_pdf = out / f"p{index + 1}.pdf"
        if not page_pdf.is_file():
            try:
                render_page(
                    pdf_path,
                    blocks,
                    index,
                    page_pdf,
                    font_path=font_path,
                    heading_font_path=heading_font_path,
                    body_size=page_sizes[index + 1],
                    line_height=line_height,
                    min_size=min_size,
                    max_size=max_size,
                    sidebar_size=page_sidebar_sizes.get(
                        index + 1, SIDEBAR_MIN_SIZE
                    ),
                )
            except Exception as exc:
                log.warning("译文页重渲染失败 p%d：%s", index + 1, exc)
                _copy_original_page(pdf_path, index, page_pdf)
                failed_pages.add(index + 1)
        try:
            doc = pymupdf.open(page_pdf)
            merged.insert_pdf(doc)
            doc.close()
        except Exception as exc:
            # 页文件损坏/被占用：用原页占位，保证合并后的页数与尺寸不变。
            log.warning("译文页合并失败 p%d：%s", index + 1, exc)
            _copy_original_page(pdf_path, index, page_pdf)
            failed_pages.add(index + 1)
            doc = pymupdf.open(page_pdf)
            merged.insert_pdf(doc)
            doc.close()
    try:
        merged.subset_fonts()
    except Exception:
        pass
    merged.save(out / "translated.pdf.tmp", garbage=4, deflate=True, clean=True)
    merged.close()
    final_pdf = _commit_pdf(out / "translated.pdf.tmp", out / "translated.pdf")

    check = pymupdf.open(final_pdf)
    merged_sizes = [
        (round(check[i].rect.width, 1), round(check[i].rect.height, 1))
        for i in range(check.page_count)
    ]
    check.close()

    pages_reused = sum(1 for slot in pages_info if slot["reused"])
    result = {
        "cached": False,
        "lang": lang,
        "pages": page_count,
        "page_size_ok": merged_sizes == sizes,
        "body_size_requested": requested,
        "body_size_used": size,
        "page_sizes": {str(p): s for p, s in sorted(page_sizes.items())},
        "pages_reused": pages_reused,
        "pages_rendered": page_count - pages_reused,
        "failed_pages": sorted(failed_pages),
        "totals": totals,
        "detail": detail,
    }
    # 构建报告：交付判据。四列覆盖率分开计账，不合并成单一数字。
    body_blocks = sum(int(row.get("body_blocks") or 0) for row in detail)
    side_blocks = sum(int(row.get("sidebar_blocks") or 0) for row in detail)
    body_preserved = [
        {"page": row.get("page"), "block": block_id}
        for row in detail
        for block_id in (row.get("overflow") or [])
    ]
    side_preserved = [
        {"page": row.get("page"), "block": block_id}
        for row in detail
        for block_id in (row.get("sidebar_overflow") or [])
    ]
    conflict_preserved = [
        {"page": row.get("page"), "block": item.get("block_id"), "reason": "sidebar_conflict"}
        for row in detail
        for item in (row.get("skipped") or [])
        if isinstance(item, dict) and item.get("reason") == "sidebar_conflict"
    ]
    cross_leaks = [
        {"page": row.get("page"), "block": block_id}
        for row in detail
        for block_id in (row.get("cross_leaks") or [])
    ]
    coverage = {
        "body": (
            round(max(0.0, 1 - len(body_preserved) / body_blocks), 4)
            if body_blocks
            else 1.0
        ),
        "sidebar": (
            round(max(0.0, 1 - len(side_preserved) / side_blocks), 4)
            if side_blocks
            else 1.0
        ),
        "table": None,
    }
    build_report = {
        "algo_ver": PDF_ALGO_VER,
        "pages": page_count,
        "page_sizes_ok": result["page_size_ok"],
        "body_size_used": size,
        "sidebar_size_used": min_side_font,
        "page_sizes": {str(p): s for p, s in sorted(page_sizes.items())},
        "page_sidebar_sizes": {
            str(p): s for p, s in sorted(page_sidebar_sizes.items())
        },
        "coverage": coverage,
        "overlap_pairs": int(overlap_count),
        "cross_column_leaks": cross_leaks,
        "sidebar_overflow": side_preserved,
        "preserved": body_preserved + conflict_preserved,
        "failed_pages": sorted(failed_pages),
    }
    (out / "build_report.json").write_text(
        json.dumps(build_report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    result["build_report"] = build_report

    pages_meta_path.write_text(
        json.dumps(
            {
                "algo_ver": PDF_ALGO_VER,
                "lang": lang,
                "body_size_used": size,
                "pages": {
                    str(slot["page"]): {
                        "hash": slot["hash"],
                        "size": slot["size"],
                        "sidebar_size": slot.get("sidebar_size"),
                        "detail": slot["detail"],
                        "file": f"p{slot['page']}.pdf",
                    }
                    for slot in pages_info
                },
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    meta_path.write_text(
        json.dumps(
            {
                "cache_key": cache_key or "",
                "algo_ver": PDF_ALGO_VER,
                "lang": lang,
                "pages": page_count,
                "page_size_ok": result["page_size_ok"],
                "body_size_used": size,
                "totals": totals,
                "pages_reused": pages_reused,
                "pages_rendered": page_count - pages_reused,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return result

def load_blocks(path: Path | str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("blocks") or []
    return [b for b in data if isinstance(b, dict)]