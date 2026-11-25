"""导出综述 / 论文 Word 时套用统一的中文排版。

pandoc 生成的 docx 只带默认的西文样式，中文会落到 Calibri 之类的字体上。
这里在导出后统一改样式与段落格式：

- 正文：小四（12pt）仿宋，西文与数字 Times New Roman，1.2 倍行距，首行缩进 2 字符；
- 对齐：两端对齐（Word 的 justify 本身不会拉伸段落末行，末行保持左对齐）；
- 标题：一级三号黑体加粗、二级小三黑体加粗、三级四号宋体加粗。

排版只是“尽量规范”，失败时不应影响导出本身，调用方负责兜底。
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

BODY_FONT = "仿宋"
WESTERN_FONT = "Times New Roman"
BODY_SIZE_PT = 12.0  # 小四
LINE_SPACING = 1.2
FIRST_LINE_INDENT_CHARS = 2

# 标题层级 -> (中文字体, 字号pt)。四级及以下按三级规格兜底。
HEADING_SPECS: dict[int, tuple[str, float]] = {
    1: ("黑体", 16.0),  # 三号
    2: ("黑体", 15.0),  # 小三
    3: ("宋体", 14.0),  # 四号
}
HEADING_FALLBACK = ("宋体", BODY_SIZE_PT)  # 小四

HEADING_PREFIXES = ("heading", "标题")
# 图表题保持原有居中/斜体，不改对齐。
CAPTION_STYLES = ("caption", "题注", "图片说明")
# 图表题、列表项、代码块不套首行缩进，避免破坏原有版式。
NO_INDENT_STYLES = (
    "caption",
    "image caption",
    "table caption",
    "题注",
    "图片说明",
    "compact",
    "source code",
    "verbatim",
)


def _heading_level(style_name: str) -> int | None:
    lowered = (style_name or "").strip().lower()
    for prefix in HEADING_PREFIXES:
        if lowered.startswith(prefix):
            tail = lowered[len(prefix):].strip()
            if tail.isdigit():
                return int(tail)
    return None


def _set_rfonts(element, chinese: str, western: str = WESTERN_FONT) -> None:
    """把中西文字体同时写进 rPr，否则 Word 只改西文、中文仍是默认字体。"""
    rpr = element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), western)
    rfonts.set(qn("w:hAnsi"), western)
    rfonts.set(qn("w:eastAsia"), chinese)


def _apply_doc_defaults(document) -> None:
    """文档默认字体：中文仿宋、西文 Times New Roman、小四。"""
    styles = document.styles.element
    doc_defaults = styles.find(qn("w:docDefaults"))
    if doc_defaults is None:
        doc_defaults = OxmlElement("w:docDefaults")
        styles.insert(0, doc_defaults)
    rpr_default = doc_defaults.find(qn("w:rPrDefault"))
    if rpr_default is None:
        rpr_default = OxmlElement("w:rPrDefault")
        doc_defaults.insert(0, rpr_default)
    rpr = rpr_default.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        rpr_default.append(rpr)
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), WESTERN_FONT)
    rfonts.set(qn("w:hAnsi"), WESTERN_FONT)
    rfonts.set(qn("w:eastAsia"), BODY_FONT)
    size = rpr.find(qn("w:sz"))
    if size is None:
        size = OxmlElement("w:sz")
        rpr.append(size)
    size.set(qn("w:val"), str(int(BODY_SIZE_PT * 2)))


def _normalized(value: str) -> str:
    return re.sub(r"[\s_-]", "", (value or "").lower())


def _style(document, *names: str):
    """按名称或 styleId 找段落样式；pandoc 与 Word 的标题样式名大小写不一致。"""
    wanted = {_normalized(name) for name in names if name}
    for style in document.styles:
        if _normalized(style.name) in wanted or _normalized(style.style_id) in wanted:
            return style
    return None


def _format_style(style, chinese: str, size_pt: float, *, bold: bool | None = None) -> None:
    style.font.name = WESTERN_FONT
    style.font.size = Pt(size_pt)
    if bold is not None:
        style.font.bold = bold
    _set_rfonts(style.element, chinese)


def _apply_body_style(style) -> None:
    if style is None:
        return
    _format_style(style, BODY_FONT, BODY_SIZE_PT, bold=False)
    fmt = style.paragraph_format
    fmt.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    fmt.line_spacing = LINE_SPACING
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(0)


def _apply_heading_styles(document) -> None:
    for level, (chinese, size_pt) in HEADING_SPECS.items():
        style = _style(document, f"Heading {level}", f"标题 {level}", f"Heading{level}")
        if style is None:
            continue
        _format_style(style, chinese, size_pt, bold=True)
    for level in (4, 5, 6):
        style = _style(document, f"Heading {level}", f"标题 {level}", f"Heading{level}")
        if style is None:
            continue
        _format_style(style, HEADING_FALLBACK[0], HEADING_FALLBACK[1], bold=True)


def _style_name(paragraph) -> str:
    try:
        return paragraph.style.name or ""
    except Exception:  # noqa: BLE001 - 样式缺失时不阻断导出
        return ""


def _apply_body_paragraph(paragraph) -> None:
    name = _style_name(paragraph).strip().lower()
    fmt = paragraph.paragraph_format
    if not any(key in name for key in CAPTION_STYLES):
        fmt.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    fmt.line_spacing = LINE_SPACING
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(0)
    if not paragraph.text.strip() or any(key in name for key in NO_INDENT_STYLES):
        return
    fmt.first_line_indent = Pt(FIRST_LINE_INDENT_CHARS * BODY_SIZE_PT)
    # 同时写 firstLineChars，Word 里显示为「首行缩进 2 字符」而不是固定磅值。
    ind = paragraph._p.get_or_add_pPr().get_or_add_ind()
    ind.set(qn("w:firstLineChars"), str(FIRST_LINE_INDENT_CHARS * 100))


def apply_review_layout(path: Path | str) -> None:
    """就地给导出后的 docx 套用中文论文排版。"""
    target = Path(path)
    document = Document(str(target))
    _apply_doc_defaults(document)
    _apply_body_style(_style(document, "Normal"))
    for name in ("Body Text", "First Paragraph"):
        _apply_body_style(_style(document, name))
    _apply_heading_styles(document)
    for paragraph in document.paragraphs:
        if _heading_level(_style_name(paragraph)):
            continue
        _apply_body_paragraph(paragraph)
    document.save(str(target))
