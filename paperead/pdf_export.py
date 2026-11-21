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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pymupdf

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



DEFAULT_LINE_HEIGHT = 1.15
DEFAULT_MIN_LINE_HEIGHT = 1.0
# 块内空白填充：默认 1.15，最多拉到 1.4，字号保持不变
MAX_FILL_LINE_HEIGHT = 1.4
BODY_FONT_SIZE = 8.6
# 自动字号：从这里开始试排，收敛到「全篇正文都能放下的最大统一字号」
AUTO_BODY_SIZE = 11.5
# 自动字号的候选档位（从高到低试，取第一个全篇统一且不溢出的）。
SIZE_STEPS = (12.0, 11.5, 10.5, 9.5, 8.6, 8.25, 7.75, 7.6)
# 译文 PDF 缓存版本：排版算法变更时递增，使旧缓存失效
PDF_ALGO_VER = "p2-autofit-v22"
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


def is_same_text(block: dict) -> bool:
    """译文与原文相同（URL、编号、纯符号），回填没有意义，直接保留原文。"""
    src = " ".join((block.get("text") or "").split())
    dst = " ".join((block.get("translated") or "").split())
    return bool(src) and src == dst


def is_formula_echo(block: dict) -> bool:
    """模型把公式行原样回抄（译文几乎没有中文），应按公式行保留原文。

    `b0056` 的 translated 是 `Attnk(hi, hj) = softmax(aij) · vj`，没有翻译；
    若按正文回填，公式裁图会被拆成两段、文本只剩 `Attn ) = softmax(`。
    """
    src = (block.get("text") or "").strip()
    dst = (block.get("translated") or "").strip()
    if not src or not dst:
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
_SYMBOL_FALLBACK = {
    "∈": "属于",
    "✓": "√",
    "∥": "||",
    "∩": "交",
    "∪": "并",
    "✉": "@",
    "≪": "<<",
    "≫": ">>",
    "\u202f": " ",
    "\t": " ",
}


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
            bbox = token["meta"].get("bbox")
            if not bbox:
                continue
            key = tuple(round(float(v), 1) for v in bbox)
            pix = pixmaps.get(key)
            if pix is None:
                continue
            rect = pymupdf.Rect(bbox)
            # 单行水平式按 1.05×字号；含上下标的 bbox 更高，按 1.18×字号，
            # 否则压缩后整图会偏上。
            height = size * (1.18 if rect.height > size * 1.25 else 1.05)
            width = height * (rect.width / max(rect.height, 1))
            atoms.append({"type": "math", "pixmap": pix, "w": width, "h": height})
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
        if kind == "caption" and any(
            _kb.get("kind") in ("table", "figure")
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
            and rect.height < 6
        ):
            result.skipped.append(
                {"block_id": block.get("id"), "reason": "too_small"}
            )
            continue
        if skip_nested_math and _contains_inner_math(rect, keep_blocks):
            result.skipped.append({"block_id": block.get("id"), "reason": "nested_math"})
            continue
        targets.append((block, rect))

    # 短标签（节标题/小标题）先占位，长段落再避让：长段先写时短标签已无处
    # 可下推，只能压在长段首行上（Abstract 标题与摘要正文即此形态）。
    targets.sort(
        key=lambda item: (
            item[1].height > 16,
            int(item[0].get("order") or 0),
        )
    )

    # 每个目标块：向下借用同栏空隙，再把公式/图表当成禁排区切成横带。
    # 只擦横带内的文字，公式本身既不被擦除，也不会被译文压住。
    bands_by_block: dict[int, list[pymupdf.Rect]] = {}
    redact_bands_by_block: dict[int, list[pymupdf.Rect]] = {}
    obstacles_by_block: dict[int, list[pymupdf.Rect]] = {}
    layout_rects: dict[int, pymupdf.Rect] = {}
    # 已处理块的「原始 bbox」：下推只按真实文字范围算，不能用被 next_limit
    # 向下扩过的 layout_rect，否则会把后块推得过远、引发新的溢出。
    placed_rects: list[pymupdf.Rect] = []
    for block, rect in targets:
        limit = next_limit(rect, page_blocks, page.rect.height)
        # 短标签只有一行，向下借用会把下方整段算进自己的范围，后块因此
        # 无处下推（Abstract 标题压住摘要正文即此形态）。
        if rect.height <= 16:
            limit = rect.y1
        # 解析 bbox 有时只盖段落首行（b0046 400–486 实际到 619）；
        # 用该块所在 x 范围内文字的实际最大 y 补足，但不得越过下一段非图表块。
        words_max = rect.y1
        if block.get("kind") == "para":
            next_non_keep = min(
                [
                    other.y0
                    for keep_block, other in page_blocks
                    if keep_block.get("kind") not in KEEP_KINDS
                    and other.y0 >= rect.y1 - 2
                    and _overlaps_x(rect, other)
                ]
                + [page.rect.height - PAGE_BOTTOM_MARGIN]
            )
            words = page.get_text(
                "words",
                clip=pymupdf.Rect(rect.x0, rect.y0, rect.x1, next_non_keep),
            )
            if words:
                words_max = max(float(word[3]) for word in words)
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
        # 小标题/短标签的 bbox 往往只有文字自身宽度（"B. GNNs" 仅 37.9pt），
        # 译文稍长就溢出，被擦掉后原地留白。右侧同栏为空时扩到同栏右缘。
        if layout_rect.height <= 16 and rect.width < page.rect.width * 0.5:
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
        if shift_y0 > layout_rect.y0 + 0.5 and shift_y0 < layout_rect.y1 - 2:
            layout_rect = pymupdf.Rect(
                layout_rect.x0, shift_y0, layout_rect.x1, layout_rect.y1
            )
        placed_rects.append(rect)

        obstacles = [
            _inflate(pymupdf.Rect(other))
            for _kb, other in keep_blocks
            # figure 的 bbox 常把图注一起罩住（旧产物尤其明显），图片本身
            # 不会被 redact 擦除，不需要作为文字禁排区。
            if _kb.get("kind") != "figure"
            and not (pymupdf.Rect(other) & layout_rect).is_empty
        ]
        # 有行内公式的块一律交给 token 流内联：公式不再作为禁排区切碎横带，
        # 原公式区域随之被擦除（含矢量字形），只保留一次内联裁图。
        if not block.get("inline_math"):
            for item in block.get("inline_math") or []:
                bbox = item.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                r = pymupdf.Rect(bbox)
                if not (r & rect).is_empty:
                    obstacles.append(_inflate(r))
        bands_by_block[id(block)] = plan_bands(layout_rect, obstacles)
        # 擦除用全部横带（含窄带），排版仍只用宽横带
        redact_bands_by_block[id(block)] = plan_bands(
            layout_rect, obstacles, min_width_ratio=0.0, min_height=0.5
        )
        obstacles_by_block[id(block)] = obstacles
        layout_rects[id(block)] = layout_rect

    inline_pixmaps: dict[int, dict[tuple, pymupdf.Pixmap]] = {}
    # 被后块借用了富余高度的前块：不得再把行距拉伸到 1.4 填空白，
    # 否则会把手伸进后块的位置、两段中文交错重叠。
    # 集合在预排版阶段填充，但 dry_run 也走下面的排版循环，须先初始化。
    no_fill_ids: set[int] = set()
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
            if kind == "title":
                size = round(body_size * TITLE_RATIO, 2)
                use_font = heading_font
            elif kind == "heading":
                size = round(
                    body_size
                    * HEADING_RATIOS.get(int(block.get("level") or 1), 1.12),
                    2,
                )
                use_font = heading_font
            else:
                size = body_size
                use_font = font
            bands = bands_by_block[id(block)]
            if (
                block.get("inline_math")
                and len(bands) == 1
                and inline_pixmaps.get(id(block))
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
                    min_size=min_size,
                    line_height=line_height,
                )
                planned[id(block)] = (size, use_font, ilh, ilines, True)
                overflow = inline_overflow
            else:
                text = clean_translation(
                    page_text, block.get("inline_math")
                )
                _lsize, llh, placements, remaining = layout_in_bands(
                    use_font,
                    text,
                    bands,
                    size,
                    min_size=min_size,
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
        for block, rect in targets:
            if id(block) not in preserved_ids:
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
                min_size=min_size,
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
            no_fill_ids.add(id(prev_block))
            planned[id(block)] = (size, use_font, nlh, nplacements, False)
        for block, rect in targets:
            if id(block) in preserved_ids:
                continue
            # 用 layout_rect（从段首到下一块上沿）的横带擦除，而不是窄 bbox。
            # 解析出的 bbox 有时只覆盖段落首行（实测 b0083 只有 10pt，段落实际
            # 245pt），只擦 bbox 会留下整段英文，译文再写上去就是中英重叠。
            for band in redact_bands_by_block[id(block)]:
                page.add_redact_annot(_inflate(band))
            if not redact_bands_by_block[id(block)] and not block.get("inline_math"):
                page.add_redact_annot(_inflate(rect))
            # 横带在公式附近可能切得不够细，逐词补擦非公式文字，
            # 否则会留下 Figure 1:/Given a... 这类英文碎片。
            obstacles = obstacles_by_block.get(id(block)) or []

            if obstacles:
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
        # 内联裁图随后写入，覆盖白块，页面上只看到一次公式。
        for block, rect in targets:
            if id(block) in preserved_ids:
                continue
            if not block.get("inline_math"):
                continue
            for item in block.get("inline_math") or []:
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
        # 每页只注册一次字体：每行都带 fontfile 会把整套字体重复嵌入，
        # 单页能从几百 KB 膨胀到近 10MB。
        page.insert_font(fontname="zhfs", fontfile=str(font_file))
        page.insert_font(fontname="zhhei", fontfile=str(heading_file))

    for block, rect in targets:
        if not dry_run and id(block) in preserved_ids:
            layout = BlockLayout(
                block_id=str(block.get("id")),
                kind=str(block.get("kind")),
                source_size=round(body_size, 2),
                font_size=round(body_size, 2),
                lines=0,
                needed=0.0,
                available=round(layout_rects[id(block)].height, 2),
                overflow=True,
                note="译文放不下，整块保留原文",
            )
            result.layouts.append(layout)
            continue
        page_text = _strip_heading_echo(
            _slice_translation(block, page_index), page_blocks, block
        )
        text = clean_translation(page_text, block.get("inline_math"))
        kind = block.get("kind")
        if kind == "title":
            size, fontname, use_font = (
                round(body_size * TITLE_RATIO, 2),
                "zhhei",
                heading_font,
            )
        elif kind == "heading":
            level = int(block.get("level") or 1)
            size = round(body_size * HEADING_RATIOS.get(level, 1.12), 2)
            fontname, use_font = "zhhei", heading_font
        else:
            size, fontname, use_font = body_size, "zhfs", font
        bands = bands_by_block[id(block)]
        layout_rect = layout_rects[id(block)]

        # 单横带 + 有行内公式：走 token 流混排，公式裁图回到句子里。
        # 多横带（表格/公式绕排）暂时沿用纯文本排版。
        if (
            not dry_run
            and block.get("inline_math")
            and len(bands) == 1
            and inline_pixmaps.get(id(block))
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
                min_size=min_size,
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
                    page.insert_image(image_rect, pixmap=atom["pixmap"])
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
        fit_size, fit_lh, placements, remaining = layout_in_bands(
            use_font,
            text,
            bands,
            size,
            min_size=min_size,
            max_size=max(max_size, size),
            line_height=line_height,
        )
        full_lines = wrap_text(use_font, text, max(rect.width - 1, 8), fit_size)
        needed = (use_font.ascender - use_font.descender) * fit_size + max(
            0, len(full_lines) - 1
        ) * fit_size * fit_lh
        # 中文通常比英文短：字号已经是全篇统一的上限后，块内仍会剩空白。
        # 这里只拉伸行距（最多 1.4 倍）填掉剩余高度，不动字号，也不越到下一条横带。
        if (
            kind in ("para", "abstract")
            and len(bands) == 1
            and len(placements) == 1
            and not block.get("inline_math")
            and id(block) not in no_fill_ids
        ):
            line_count = len(placements[0][1])
            if line_count > 1:
                band = placements[0][0]
                available = min(layout_rect.height, band.height)
                em = (use_font.ascender - use_font.descender) * fit_size
                spare = available - needed - 2.0
                if spare > 0:
                    filled = fit_lh + spare / ((line_count - 1) * fit_size)
                    fit_lh = round(min(MAX_FILL_LINE_HEIGHT, filled), 3)
                    needed = em + (line_count - 1) * fit_size * fit_lh
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
                )
            }
        )
    raw = json.dumps(
        {
            "page": page_index + 1,
            "lang": lang,
            "size": round(float(size), 3),
            "font": str(font_path or ""),
            "heading_font": str(heading_font_path or ""),
            "algo": PDF_ALGO_VER,
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


def _page_detail(page_pdf: Path, res: Any) -> dict[str, Any]:
    doc = pymupdf.open(page_pdf)
    page = doc[0]
    text = page.get_text()
    overlap = _count_overlap(page)
    doc.close()
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return {
        "page": res.page + 1,
        "blocks": len(res.layouts),
        "overflow": [x.block_id for x in res.layouts if x.overflow],
        "residue": res.residue_blocks,
        "skipped": res.skipped,
        "overlap": overlap,
        "cjk": cjk,
    }


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
    """逐页回填并合并为整篇译文 PDF。

    必须逐页 ``insert_pdf`` 合并：多页 ``doc.save()`` 会丢掉之后写入的
    overlay 文字（中文会整篇消失），这是 P1 已确认的硬约束。

    增量策略：每页按「原文 + 译文 + 字号 + 算法版本」取哈希，命中时直接复用
    ``p{n}.pdf``；只有译文变化的页才重新渲染。字号仍按全篇统一：若变化页
    需要更小字号，则回退到全量重排。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

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

    def _run(
        size_value: float, target_dir: Path
    ) -> tuple[list[dict[str, Any]], float, int, int]:
        """渲染全篇，返回 (页信息, 最小正文字号, 溢出块数, 中文重叠对数)。"""
        info: list[dict[str, Any]] = []
        smallest = size_value
        overflow_total = 0
        overlap_total = 0
        for index in range(page_count):
            page_pdf = target_dir / f"p{index + 1}.pdf"
            digest = _page_hash(
                blocks,
                index,
                lang=lang,
                size=size_value,
                font_path=font_path,
                heading_font_path=heading_font_path,
            )
            cached = cached_pages.get(str(index + 1)) or {}
            if (
                target_dir == out
                and cached.get("hash") == digest
                and abs(float(cached.get("size", -1)) - size_value) < 0.001
                and page_pdf.is_file()
                and cached.get("detail")
            ):
                cached_detail = cached["detail"]
                overflow_total += len(cached_detail.get("overflow") or [])
                overlap_total += int(cached_detail.get("overlap") or 0)
                info.append(
                    {
                        "page": index + 1,
                        "hash": digest,
                        "size": size_value,
                        "reused": True,
                        "detail": cached_detail,
                    }
                )
                continue
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
            )
            detail = _page_detail(page_pdf, res)
            overlap_total += int(detail.get("overlap") or 0)
            for layout in res.layouts:
                if layout.kind in ("heading", "title"):
                    continue
                smallest = min(smallest, layout.font_size)
                if layout.overflow:
                    overflow_total += 1
            info.append(
                {
                    "page": index + 1,
                    "hash": digest,
                    "size": size_value,
                    "reused": False,
                    "detail": detail,
                }
            )
        return info, smallest, overflow_total, overlap_total

    # 第一步沿用「块内压到最小可行字号、全篇跟着降」的收敛：即使个别块
    # 物理上放不下，也不会因此把整篇降到最低档。
    size = known_size or requested
    pages_info, min_font, overflow_count, overlap_count = _run(size, out)
    for _ in range(2):
        if min_font >= size - 0.01 and overlap_count == 0:
            break
        size = max(6.5, round(min_font if min_font < size - 0.01 else size - 0.5, 2))
        cached_pages = {}
        pages_info, min_font, overflow_count, overlap_count = _run(size, out)

    # 第二步向上试更大档位：字号越高，同栏前块的译文越长、留给后一块的
    # 富余越少，所以「某块被压到 8.25」不代表 9.5 不可行（实测 9.5 反而
    # 能完整放下）。只在「没有块被压缩且溢出数不增加」时才采用。
    for step in (
        candidate for candidate in SIZE_STEPS if candidate > size + 0.01
    ):
        probe_dir = out / "_probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        trial_pages, trial_font, trial_overflow, trial_overlap = _run(step, probe_dir)
        if (
            trial_font >= step - 0.01
            and trial_overflow <= overflow_count
            and trial_overlap == 0
        ):
            for slot in trial_pages:
                (probe_dir / f"p{slot['page']}.pdf").replace(
                    out / f"p{slot['page']}.pdf"
                )
            size, pages_info, min_font, overflow_count, overlap_count = (
                step,
                trial_pages,
                trial_font,
                trial_overflow,
                trial_overlap,
            )
            break
        for leftover in probe_dir.glob("p*.pdf"):
            leftover.unlink()

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
            render_page(
                pdf_path,
                blocks,
                index,
                page_pdf,
                font_path=font_path,
                heading_font_path=heading_font_path,
                body_size=size,
                line_height=line_height,
                min_size=min_size,
                max_size=max_size,
            )
        doc = pymupdf.open(page_pdf)
        merged.insert_pdf(doc)
        doc.close()
    try:
        merged.subset_fonts()
    except Exception:
        pass
    merged.save(out / "translated.pdf", garbage=4, deflate=True, clean=True)
    merged.close()

    check = pymupdf.open(out / "translated.pdf")
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
        "pages_reused": pages_reused,
        "pages_rendered": page_count - pages_reused,
        "totals": totals,
        "detail": detail,
    }
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