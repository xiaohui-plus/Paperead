"""Word (.docx) 读取与编辑。

研究 agent 只能处理文本，这里把 docx 拆成带序号的段落 / 表格供它阅读，
再按操作列表修改原文件（尽量保留未涉及的原有格式），前端用 docx-preview 预览结果。
覆盖：文字/段落、字体排版、标题与自定义样式、表格（新建/行列/合并/边框/宽度）、
页面设置、页眉页脚页码、分页分节、列表、图片、超链接和目录域。

段落序号以最近一次 ``read_docx`` 返回的列表为准；同一次编辑里所有 ``index``
都指向这份原始列表，插入 / 删除不会改变其他操作对序号的引用。
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Pt, RGBColor
from docx.text.paragraph import Paragraph

MAX_READ_CHARS = 60000
MAX_PARAGRAPH_CHARS = 4000
MAX_CELL_CHARS = 800

HEADING_PREFIXES = ("heading", "标题")

ALIGNMENTS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}

# format_paragraph 支持的排版字段：run 级（字体）+ 段落级（缩进/行距）。
RUN_FORMAT_KEYS = ("font", "fontSize", "bold", "italic", "underline", "color")
PARAGRAPH_FORMAT_KEYS = (
    "align",
    "firstLineIndentChars",
    "firstLineIndentPt",
    "leftIndentPt",
    "lineSpacing",
    "spaceBeforePt",
    "spaceAfterPt",
)
FORMAT_KEYS = RUN_FORMAT_KEYS + PARAGRAPH_FORMAT_KEYS

COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")


class DocxError(ValueError):
    """docx 读写失败：消息直接回给 agent，说明该怎么改。"""


def _style_name(paragraph: Paragraph) -> str:
    try:
        return paragraph.style.name or ""
    except Exception:  # noqa: BLE001 - 样式定义缺失时不阻断读取
        return ""


def _style_name_for_table(table) -> str:
    try:
        return (table.style.name or "") if table.style is not None else ""
    except Exception:  # noqa: BLE001 - 表格样式缺失时不阻断读取
        return ""


def _heading_level(style_name: str) -> int | None:
    lowered = style_name.strip().lower()
    for prefix in HEADING_PREFIXES:
        if lowered.startswith(prefix):
            tail = lowered[len(prefix):].strip()
            if tail.isdigit():
                return int(tail)
    return None


def _open(path: Path):
    if not path.is_file():
        raise DocxError(f"找不到文件：{path.name}")
    if path.suffix.lower() != ".docx":
        raise DocxError("只支持 .docx；旧版 .doc 请先在 Word 里另存为 .docx")
    try:
        return Document(str(path))
    except Exception as error:  # noqa: BLE001 - 加密/损坏文件要给出可读原因
        raise DocxError(f"无法解析 docx：{error}") from error


def read_docx(path: Path) -> dict[str, Any]:
    """返回段落与表格的结构化内容，供 agent 定位要改的地方。"""
    document = _open(path)
    budget = MAX_READ_CHARS
    paragraphs: list[dict[str, Any]] = []
    outline: list[dict[str, Any]] = []
    truncated = False

    for index, paragraph in enumerate(document.paragraphs):
        text = paragraph.text
        if len(text) > MAX_PARAGRAPH_CHARS:
            text = text[:MAX_PARAGRAPH_CHARS] + "…"
        if budget - len(text) < 0:
            truncated = True
            break
        budget -= len(text)
        style = _style_name(paragraph)
        paragraphs.append({"index": index, "style": style, "text": text})
        level = _heading_level(style)
        if level:
            outline.append({"index": index, "level": level, "text": paragraph.text})

    tables: list[dict[str, Any]] = []
    for table_index, table in enumerate(document.tables):
        rows: list[list[str]] = []
        for row in table.rows:
            cells = []
            for cell in row.cells:
                text = cell.text
                if len(text) > MAX_CELL_CHARS:
                    text = text[:MAX_CELL_CHARS] + "…"
                cells.append(text)
                budget -= len(text)
            rows.append(cells)
        tables.append(
            {
                "index": table_index,
                "rows": rows,
                "columns": len(table.columns),
                "style": _style_name_for_table(table),
            }
        )

    result: dict[str, Any] = {
        "file": {"name": path.name, "size": path.stat().st_size},
        "paragraphs": paragraphs,
        "tables": tables,
        "outline": outline,
        "totalParagraphs": len(document.paragraphs),
        "totalTables": len(document.tables),
        "truncated": truncated,
    }
    if truncated:
        result["note"] = "只返回了前面一部分内容；段落 index 仍按原文档排序。"
    return result


def _set_paragraph_text(paragraph: Paragraph, text: str) -> None:
    """替换段落文字，保留第一个 run 的字体格式与段落样式。"""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for run in runs[1:]:
            run._element.getparent().remove(run._element)
    else:
        paragraph.add_run(text)


def _replace_text(paragraph: Paragraph, find: str, replacement: str) -> int:
    """在段落内替换文字：能按 run 命中就保留段内多样式，跨 run 时整段重写。"""
    whole = paragraph.text
    total = whole.count(find)
    if total == 0:
        return 0
    replaced = 0
    for run in paragraph.runs:
        if find in run.text:
            replaced += run.text.count(find)
            run.text = run.text.replace(find, replacement)
    if replaced < total:
        _set_paragraph_text(paragraph, paragraph.text.replace(find, replacement))
    return total


def _clone_paragraph(anchor: Paragraph | None, document) -> Paragraph:
    """复制相邻段落的段落属性（样式、缩进、编号），用于插入新段落。"""
    if anchor is None:
        new_paragraph = document.add_paragraph()
        first = document.paragraphs[0]
        if first._p is not new_paragraph._p:
            first._p.addprevious(new_paragraph._p)
        return new_paragraph
    element = copy.deepcopy(anchor._p)
    for child in list(element):
        if child.tag != qn("w:pPr"):
            element.remove(child)
    anchor._p.addnext(element)
    return Paragraph(element, anchor._parent)


def _apply_style(paragraph: Paragraph, style: str | None) -> None:
    if not style:
        return
    try:
        paragraph.style = style
    except (KeyError, ValueError) as error:
        raise DocxError(
            f"文档里没有样式「{style}」；可先用 docx_read 查看现有段落的 style，或省略 style。"
        ) from error


def _cell_paragraphs(cell) -> list[Paragraph]:
    return list(cell.paragraphs)


def _number_value(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DocxError(f"{name} 需要数字（当前：{value!r}）。")
    number = float(value)
    if number < low or number > high:
        raise DocxError(f"{name} 超出范围：应在 {low}～{high} 之间（当前：{value}）。")
    return number


def _bool_value(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise DocxError(f"{name} 需要 true / false（当前：{value!r}）。")
    return value


def _first_run_size(paragraph: Paragraph) -> float | None:
    for run in paragraph.runs:
        if run.font.size is not None:
            return run.font.size.pt
    try:
        size = paragraph.style.font.size
    except Exception:  # noqa: BLE001 - 样式缺失时按默认字号
        return None
    return size.pt if size is not None else None


def _apply_run_format(paragraph: Paragraph, spec: dict[str, Any]) -> None:
    """字体相关属性写到每个 run；中文还要补 w:eastAsia，否则 Word 只换西文字体。"""
    keys = [key for key in RUN_FORMAT_KEYS if key in spec]
    if not keys:
        return
    for run in paragraph.runs:
        if "font" in spec:
            run.font.name = spec["font"]
            r_pr = run._element.get_or_add_rPr()
            r_pr.get_or_add_rFonts().set(qn("w:eastAsia"), spec["font"])
        if "fontSize" in spec:
            run.font.size = Pt(spec["fontSize"])
        if "bold" in spec:
            run.font.bold = spec["bold"]
        if "italic" in spec:
            run.font.italic = spec["italic"]
        if "underline" in spec:
            run.font.underline = spec["underline"]
        if "color" in spec:
            run.font.color.rgb = RGBColor.from_string(spec["color"].upper())


def _apply_paragraph_format(paragraph: Paragraph, spec: dict[str, Any]) -> None:
    """段落级排版：对齐、缩进、行距、段距。"""
    fmt = paragraph.paragraph_format
    if "align" in spec:
        fmt.alignment = ALIGNMENTS[spec["align"]]
    if "lineSpacing" in spec:
        fmt.line_spacing = spec["lineSpacing"]
    if "spaceBeforePt" in spec:
        fmt.space_before = Pt(spec["spaceBeforePt"])
    if "spaceAfterPt" in spec:
        fmt.space_after = Pt(spec["spaceAfterPt"])
    if "leftIndentPt" in spec:
        fmt.left_indent = Pt(spec["leftIndentPt"])

    if "firstLineIndentChars" in spec:
        chars = spec["firstLineIndentChars"]
        # Word 认字符缩进，docx-preview 只认 twips，两边都写。
        size_pt = spec.get("fontSize") or _first_run_size(paragraph) or 12.0
        fmt.first_line_indent = Pt(chars * size_pt)
        ind = paragraph._p.get_or_add_pPr().get_or_add_ind()
        ind.set(qn("w:firstLineChars"), str(int(round(chars * 100))))
    elif "firstLineIndentPt" in spec:
        fmt.first_line_indent = Pt(spec["firstLineIndentPt"])
        p_pr = paragraph._p.pPr
        ind = p_pr.find(qn("w:ind")) if p_pr is not None else None
        if ind is not None:
            ind.attrib.pop(qn("w:firstLineChars"), None)


def _format_spec(operation: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化 format_paragraph 的排版参数。"""
    spec: dict[str, Any] = {}
    for key in RUN_FORMAT_KEYS + PARAGRAPH_FORMAT_KEYS:
        if key not in operation:
            continue
        value = operation[key]
        if key == "font":
            if not isinstance(value, str) or not value.strip():
                raise DocxError("font 需要非空字符串，例如「宋体」或「Times New Roman」。")
            spec[key] = value.strip()
        elif key == "color":
            if not isinstance(value, str) or not COLOR_RE.match(value.strip()):
                raise DocxError("color 需要 6 位十六进制颜色，例如 000000。")
            spec[key] = value.strip()
        elif key == "align":
            if value not in ALIGNMENTS:
                raise DocxError("align 只支持 left / center / right / justify。")
            spec[key] = value
        elif key in {"bold", "italic", "underline"}:
            spec[key] = _bool_value(value, key)
        elif key == "fontSize":
            spec[key] = _number_value(value, key, 1, 200)
        elif key == "lineSpacing":
            spec[key] = _number_value(value, key, 0.5, 10)
        elif key == "firstLineIndentChars":
            spec[key] = _number_value(value, key, 0, 20)
        else:  # 其余都是磅值
            spec[key] = _number_value(value, key, 0, 1000)
    if not spec:
        raise DocxError(
            "format_paragraph 至少需要一个排版参数：font / fontSize / bold / italic / "
            "underline / color / align / firstLineIndentChars / firstLineIndentPt / "
            "leftIndentPt / lineSpacing / spaceBeforePt / spaceAfterPt。"
        )
    if "firstLineIndentChars" in spec and "firstLineIndentPt" in spec:
        raise DocxError("firstLineIndentChars 与 firstLineIndentPt 只能二选一。")
    return spec


def _target_indices(operation: dict[str, Any], paragraphs: list[Paragraph]) -> list[int]:
    """定位要排版的段落：单个 index、indices 数组，或 all=true 全篇。"""
    if operation.get("all") is True:
        return list(range(len(paragraphs)))
    indices = operation.get("indices")
    if indices is None:
        return [_paragraph_index(operation, paragraphs)]
    if not isinstance(indices, list) or not indices:
        raise DocxError("indices 需要非空的段落序号数组，例如 [1,2,3]。")
    targets: list[int] = []
    for value in indices:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DocxError("indices 里的每个序号都必须是整数。")
        if value < 0 or value >= len(paragraphs):
            raise DocxError(
                f"段落 index {value} 超出范围（共 {len(paragraphs)} 段），请先用 docx_read 核对。"
            )
        targets.append(value)
    return targets


def _normalized(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", (name or "").lower())


def _find_style(document, name: str):
    """按名称或 styleId 找样式；兼容「标题 1 / Heading 1 / Heading1」。"""
    wanted = _normalized(name)
    for style in document.styles:
        if wanted in {_normalized(style.name or ""), _normalized(style.style_id or "")}:
            return style
    raise DocxError(
        f"文档里没有样式「{name}」；可先用 docx_read 查看现有段落的 style，或先用 create_style 新建。"
    )


def _optional_style(document, name: str | None):
    return _find_style(document, name) if name else None


def _style_spec(operation: dict[str, Any], required: bool) -> dict[str, Any]:
    """从操作里提取排版字段；样式操作允许为空。"""
    raw = {key: operation[key] for key in FORMAT_KEYS if key in operation}
    if not raw:
        if required:
            raise DocxError("至少需要一个样式/排版参数。")
        return {}
    return _format_spec(raw)


def _apply_style_props(style, spec: dict[str, Any]) -> None:
    if "font" in spec:
        style.font.name = spec["font"]
        r_pr = style.element.get_or_add_rPr()
        r_pr.get_or_add_rFonts().set(qn("w:eastAsia"), spec["font"])
    if "fontSize" in spec:
        style.font.size = Pt(spec["fontSize"])
    if "bold" in spec:
        style.font.bold = spec["bold"]
    if "italic" in spec:
        style.font.italic = spec["italic"]
    if "underline" in spec:
        style.font.underline = spec["underline"]
    if "color" in spec:
        style.font.color.rgb = RGBColor.from_string(spec["color"].upper())

    try:
        fmt = style.paragraph_format
    except (AttributeError, ValueError):
        return
    if "align" in spec:
        fmt.alignment = ALIGNMENTS[spec["align"]]
    if "lineSpacing" in spec:
        fmt.line_spacing = spec["lineSpacing"]
    if "spaceBeforePt" in spec:
        fmt.space_before = Pt(spec["spaceBeforePt"])
    if "spaceAfterPt" in spec:
        fmt.space_after = Pt(spec["spaceAfterPt"])
    if "leftIndentPt" in spec:
        fmt.left_indent = Pt(spec["leftIndentPt"])
    if "firstLineIndentChars" in spec:
        chars = spec["firstLineIndentChars"]
        size_pt = spec.get("fontSize") or 12.0
        fmt.first_line_indent = Pt(chars * size_pt)
        ind = style.element.get_or_add_pPr().get_or_add_ind()
        ind.set(qn("w:firstLineChars"), str(int(round(chars * 100))))
    elif "firstLineIndentPt" in spec:
        fmt.first_line_indent = Pt(spec["firstLineIndentPt"])
        p_pr = style.element.pPr
        ind = p_pr.find(qn("w:ind")) if p_pr is not None else None
        if ind is not None:
            ind.attrib.pop(qn("w:firstLineChars"), None)


def _heading_style(document, level: int):
    for candidate in (
        f"Heading {level}",
        f"Heading{level}",
        f"标题 {level}",
        f"标题{level}",
    ):
        try:
            return _find_style(document, candidate)
        except DocxError:
            continue
    style = document.styles.add_style(f"Heading {level}", WD_STYLE_TYPE.PARAGRAPH)
    try:
        style.base_style = _find_style(document, "Normal")
    except DocxError:
        pass
    style.font.bold = True
    style.font.size = Pt({1: 16, 2: 14, 3: 13, 4: 12}.get(level, 12))
    return style


def _set_outline_level(paragraph: Paragraph, level: int) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    p_pr.get_or_add_outlineLvl().val = max(0, level - 1)


def _resolve_image(workspace: Path | None, value: str) -> Path:
    if workspace is None:
        raise DocxError("insert_image 需要项目工作区，无法解析图片路径。")
    if not isinstance(value, str) or not value.strip():
        raise DocxError("insert_image 需要 image（项目里的图片路径）。")
    root = workspace.resolve()
    candidate = (root / value.strip()).resolve()
    if root != candidate and root not in candidate.parents:
        raise DocxError("图片必须位于当前项目目录内。")
    if not candidate.is_file():
        raise DocxError(f"找不到图片：{value.strip()}（请先用 project_read 确认路径）。")
    return candidate


def _new_paragraph(document, originals: list[Paragraph], after: int | None) -> Paragraph:
    """新建段落并按 after 放到指定段落之后（-1 表示文首，None 表示文末）。"""
    paragraph = document.add_paragraph()
    if after is None:
        return paragraph
    if not isinstance(after, int) or isinstance(after, bool) or after < -1:
        raise DocxError("after 需要整数段落序号；用 -1 表示文首，省略表示文末。")
    if after >= len(originals):
        raise DocxError(f"after {after} 超出范围（共 {len(originals)} 段）。")
    if after == -1:
        if originals:
            originals[0]._p.addprevious(paragraph._p)
    else:
        originals[after]._p.addnext(paragraph._p)
    return paragraph


def _add_field(paragraph: Paragraph, instruction: str, placeholder: str = "") -> None:
    """插入域代码（页码 / 目录）；打开 Word 后按 F9 或打印时更新。"""
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), instruction)
    if placeholder:
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = placeholder
        run.append(text)
        field.append(run)
    paragraph._p.append(field)


def _add_hyperlink(paragraph: Paragraph, text: str, url: str) -> None:
    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(color)
    props.append(underline)
    run.append(props)
    text_el = OxmlElement("w:t")
    text_el.text = text
    text_el.set(qn("xml:space"), "preserve")
    run.append(text_el)
    link.append(run)
    paragraph._p.append(link)


def _table_display_index(document, table) -> int:
    for index, candidate in enumerate(document.tables):
        if candidate._tbl is table._tbl:
            return index
    return -1


def _table_at(document, operation: dict[str, Any]):
    index = operation.get("table")
    if isinstance(index, bool) or not isinstance(index, int):
        raise DocxError("table 需要整数表格序号（来自 docx_read 的 tables[].index）。")
    if index < 0 or index >= len(document.tables):
        raise DocxError(f"table {index} 超出范围（共 {len(document.tables)} 个表格）。")
    return document.tables[index]


def _set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.insert_element_before(
            shd,
            "w:noWrap",
            "w:tcMar",
            "w:textDirection",
            "w:tcFitText",
            "w:vAlign",
            "w:hideMark",
            "w:headers",
            "w:cellIns",
            "w:cellDel",
            "w:cellMerge",
            "w:tcPrChange",
        )
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)


def _repeat_header_row(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:tblHeader")) is not None:
        return
    header = OxmlElement("w:tblHeader")
    tr_pr.insert_element_before(
        header,
        "w:tblCellSpacing",
        "w:jc",
        "w:hidden",
        "w:ins",
        "w:del",
        "w:trPrChange",
    )


def _set_table_borders(table, preset: str) -> None:
    if preset not in {"none", "outer", "all", "grid"}:
        raise DocxError("borders 只支持 none / outer / all / grid。")
    all_edges = ("top", "left", "bottom", "right", "insideH", "insideV")
    enabled = set() if preset == "none" else {"top", "left", "bottom", "right"}
    if preset in {"all", "grid"}:
        enabled |= {"insideH", "insideV"}
    borders = OxmlElement("w:tblBorders")
    for edge in all_edges:
        el = OxmlElement(f"w:{edge}")
        if edge in enabled:
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), "4")
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), "auto")
        else:
            el.set(qn("w:val"), "nil")
        borders.append(el)
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblBorders"))
    if existing is not None:
        tbl_pr.remove(existing)
    tbl_pr.insert_element_before(
        borders,
        "w:shd",
        "w:tblLayout",
        "w:tblCellMar",
        "w:tblLook",
        "w:tblCaption",
        "w:tblDescription",
        "w:tblPrChange",
    )


TABLE_ALIGNMENTS = {
    "left": WD_TABLE_ALIGNMENT.LEFT,
    "center": WD_TABLE_ALIGNMENT.CENTER,
    "right": WD_TABLE_ALIGNMENT.RIGHT,
}


def _set_table_widths(table, widths: Any) -> None:
    if not isinstance(widths, list) or not widths:
        raise DocxError("widthsPt 需要列宽数组，例如 [120, 240, 240]。")
    if len(widths) != len(table.columns):
        raise DocxError(
            f"widthsPt 个数（{len(widths)}）应等于表格列数（{len(table.columns)}）。"
        )
    table.autofit = False
    for column_index, value in enumerate(widths):
        width = _number_value(value, "widthsPt", 1, 2000)
        for row in table.rows:
            row.cells[column_index].width = Pt(width)


def _apply_table_layout(table, document, operation: dict[str, Any]) -> None:
    if "style" in operation:
        table.style = _find_style(document, str(operation["style"]))
    if "align" in operation:
        align = operation["align"]
        if align not in TABLE_ALIGNMENTS:
            raise DocxError("表格 align 只支持 left / center / right。")
        table.alignment = TABLE_ALIGNMENTS[align]
    if "widthsPt" in operation:
        _set_table_widths(table, operation["widthsPt"])
    if "borders" in operation:
        _set_table_borders(table, str(operation["borders"]))
    if operation.get("header") is True:
        row = table.rows[0]
        _repeat_header_row(row)
        for cell in row.cells:
            _set_cell_shading(cell, "D9E2F3")
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.bold = True


def _insert_table(document, originals: list[Paragraph], operation: dict[str, Any]):
    rows = operation.get("rows")
    columns = operation.get("columns", operation.get("cols"))
    for name, value in (("rows", rows), ("columns", columns)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
            raise DocxError(f"{name} 需要 1～50 的整数（当前：{value!r}）。")
    table = document.add_table(rows=rows, cols=columns)
    data = operation.get("data")
    if data is not None:
        if not isinstance(data, list):
            raise DocxError("data 需要二维数组，例如 [[\"指标\",\"数值\"],[\"准确率\",\"85%\"]]。")
        for row_index, row_data in enumerate(data[:rows]):
            if not isinstance(row_data, list):
                raise DocxError("data 的每一行都必须是数组。")
            for column_index, value in enumerate(row_data[:columns]):
                table.cell(row_index, column_index).text = str(value)
    _apply_table_layout(table, document, operation)

    after = operation.get("after")
    if after is not None:
        if isinstance(after, bool) or not isinstance(after, int) or after < -1:
            raise DocxError("after 需要整数段落序号；用 -1 表示文首，省略表示文末。")
        if after >= len(originals):
            raise DocxError(f"after {after} 超出范围（共 {len(originals)} 段）。")
        if after == -1:
            if originals:
                originals[0]._p.addprevious(table._tbl)
        else:
            originals[after]._p.addnext(table._tbl)
    return table


def _insert_table_row(document, operation: dict[str, Any]):
    table = _table_at(document, operation)
    row = table.add_row()
    data = operation.get("data")
    if data is not None:
        if not isinstance(data, list):
            raise DocxError("data 需要数组，例如 [\"指标\",\"数值\"]。")
        for index, value in enumerate(data[: len(table.columns)]):
            row.cells[index].text = str(value)
    before = operation.get("before")
    if before is not None:
        if isinstance(before, bool) or not isinstance(before, int) or before < 0:
            raise DocxError("before 需要非负整数行号。")
        if before >= len(table.rows):
            raise DocxError(f"before {before} 超出范围（共 {len(table.rows)} 行）。")
        if before < len(table.rows) - 1:
            table.rows[before]._tr.addprevious(row._tr)
    return row


def _insert_table_column(document, operation: dict[str, Any]):
    table = _table_at(document, operation)
    table.add_column(Cm(2.0))
    data = operation.get("data")
    if data is not None:
        if not isinstance(data, list):
            raise DocxError("data 需要数组，例如 [\"表头\",\"内容\"]。")
        column = len(table.columns) - 1
        for index, value in enumerate(data[: len(table.rows)]):
            table.cell(index, column).text = str(value)
    before = operation.get("before")
    if before is not None:
        if isinstance(before, bool) or not isinstance(before, int) or before < 0:
            raise DocxError("before 需要非负整数列号。")
        if before >= len(table.columns):
            raise DocxError(f"before {before} 超出范围（共 {len(table.columns)} 列）。")
        if before < len(table.columns) - 1:
            # add_column 只能在末尾追加，先追加再移动到目标位置。
            grid = table._tbl.tblGrid
            grid_col = grid.gridCol_lst[-1]
            grid.remove(grid_col)
            grid.insert(before, grid_col)
            for row in table.rows:
                cell = row._tr.tc_lst[-1]
                row._tr.remove(cell)
                row._tr.tc_lst[before].addprevious(cell)
    return table


def _set_cell_text(cell, text: str) -> None:
    paragraphs = _cell_paragraphs(cell)
    if not paragraphs:
        cell.text = text
        return
    _set_paragraph_text(paragraphs[0], text)
    for extra in paragraphs[1:]:
        extra._element.getparent().remove(extra._element)


def _delete_table_row(document, operation: dict[str, Any]) -> None:
    table = _table_at(document, operation)
    row = operation.get("row")
    if isinstance(row, bool) or not isinstance(row, int):
        raise DocxError("row 需要整数行号。")
    if row < 0 or row >= len(table.rows):
        raise DocxError(f"row {row} 超出范围（共 {len(table.rows)} 行）。")
    if len(table.rows) <= 1:
        raise DocxError("表格至少要保留一行；如需删表请用 delete_table。")
    table._tbl.remove(table.rows[row]._tr)


def _delete_table_column(document, operation: dict[str, Any]) -> None:
    table = _table_at(document, operation)
    column = operation.get("column", operation.get("col"))
    if isinstance(column, bool) or not isinstance(column, int):
        raise DocxError("column 需要整数列号。")
    if column < 0 or column >= len(table.columns):
        raise DocxError(f"column {column} 超出范围（共 {len(table.columns)} 列）。")
    if len(table.columns) <= 1:
        raise DocxError("表格至少要保留一列；如需删表请用 delete_table。")
    grid = table._tbl.tblGrid
    grid.remove(grid.gridCol_lst[column])
    for row in table.rows:
        row._tr.remove(row._tr.tc_lst[column])


def _merge_table_cells(document, operation: dict[str, Any]) -> None:
    table = _table_at(document, operation)
    coords = []
    for key in ("startRow", "startColumn", "endRow", "endColumn"):
        value = operation.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise DocxError(f"{key} 需要整数。")
        coords.append(value)
    start_row, start_column, end_row, end_column = coords
    if not (0 <= start_row <= end_row < len(table.rows)):
        raise DocxError("合并区域的行号越界或起点在终点之后。")
    if not (0 <= start_column <= end_column < len(table.columns)):
        raise DocxError("合并区域的列号越界或起点在终点之后。")
    if start_row == end_row and start_column == end_column:
        raise DocxError("合并区域只有一个单元格，不需要合并。")
    table.cell(start_row, start_column).merge(table.cell(end_row, end_column))


def _delete_table(document, operation: dict[str, Any]) -> None:
    table = _table_at(document, operation)
    table._tbl.getparent().remove(table._tbl)


def _apply_style_to_targets(document, operation: dict[str, Any], paragraphs: list[Paragraph]) -> list[int]:
    style_name = operation.get("style")
    if not isinstance(style_name, str) or not style_name.strip():
        raise DocxError("set_style 需要 style（样式名）。")
    style = _find_style(document, style_name.strip())
    targets = _target_indices(operation, paragraphs)
    for index in targets:
        paragraphs[index].style = style
    return targets


def _apply_heading(document, operation: dict[str, Any], paragraphs: list[Paragraph]) -> list[int]:
    level = operation.get("level")
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 9:
        raise DocxError("level 需要 1～9 的整数。")
    style = _heading_style(document, level)
    targets = _target_indices(operation, paragraphs)
    for index in targets:
        paragraph = paragraphs[index]
        paragraph.style = style
        _set_outline_level(paragraph, level)
    return targets


def _create_style(document, operation: dict[str, Any]) -> str:
    name = operation.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DocxError("create_style 需要 name（样式名）。")
    name = name.strip()
    if any(_normalized(style.name or "") == _normalized(name) for style in document.styles):
        raise DocxError(f"样式「{name}」已存在，请用 modify_style 修改。")
    kind = operation.get("type", "paragraph")
    if kind not in {"paragraph", "character"}:
        raise DocxError("type 只支持 paragraph / character。")
    style = document.styles.add_style(
        name,
        WD_STYLE_TYPE.PARAGRAPH if kind == "paragraph" else WD_STYLE_TYPE.CHARACTER,
    )
    base = operation.get("base")
    if isinstance(base, str) and base.strip():
        style.base_style = _find_style(document, base.strip())
    _apply_style_props(style, _style_spec(operation, required=False))
    return style.name


def _modify_style(document, operation: dict[str, Any]) -> str:
    name = operation.get("style")
    if not isinstance(name, str) or not name.strip():
        raise DocxError("modify_style 需要 style（要修改的样式名）。")
    style = _find_style(document, name.strip())
    _apply_style_props(style, _style_spec(operation, required=True))
    return style.name


PAGE_SIZES_CM = {
    "a4": (21.0, 29.7),
    "a3": (29.7, 42.0),
    "a5": (14.8, 21.0),
    "letter": (21.59, 27.94),
    "legal": (21.59, 35.56),
}


def _select_sections(document, operation: dict[str, Any]):
    index = operation.get("section")
    if index is None:
        return list(document.sections)
    if isinstance(index, bool) or not isinstance(index, int):
        raise DocxError("section 需要整数节号（从 0 开始）。")
    if index < 0 or index >= len(document.sections):
        raise DocxError(f"section {index} 超出范围（共 {len(document.sections)} 节）。")
    return [document.sections[index]]


def _set_page(document, operation: dict[str, Any], sections) -> None:
    width_cm = height_cm = None
    size = operation.get("size")
    if size is not None:
        key = str(size).strip().lower()
        if key not in PAGE_SIZES_CM:
            raise DocxError("size 只支持 A4 / A3 / A5 / Letter / Legal，或用 widthCm+heightCm 自定义。")
        width_cm, height_cm = PAGE_SIZES_CM[key]
    if "widthCm" in operation:
        width_cm = _number_value(operation["widthCm"], "widthCm", 5, 200)
    if "heightCm" in operation:
        height_cm = _number_value(operation["heightCm"], "heightCm", 5, 200)
    if (width_cm is None) != (height_cm is None):
        raise DocxError("自定义纸张需要同时提供 widthCm 和 heightCm。")
    orientation = operation.get("orientation")

    margins = {}
    for key, attr in (
        ("marginTopCm", "top_margin"),
        ("marginBottomCm", "bottom_margin"),
        ("marginLeftCm", "left_margin"),
        ("marginRightCm", "right_margin"),
    ):
        if key in operation:
            margins[attr] = _number_value(operation[key], key, 0, 50)
    if not margins and width_cm is None and orientation is None:
        raise DocxError("set_page 至少需要 size / widthCm+heightCm / orientation / 页边距之一。")

    for section in sections:
        if width_cm is not None and height_cm is not None:
            width, height = width_cm, height_cm
            if orientation == "landscape" and width < height:
                width, height = height, width
            elif orientation != "landscape" and width > height:
                width, height = height, width
            section.page_width = Cm(width)
            section.page_height = Cm(height)
            section.orientation = (
                WD_ORIENT.LANDSCAPE if width > height else WD_ORIENT.PORTRAIT
            )
        elif orientation is not None:
            if orientation not in {"portrait", "landscape"}:
                raise DocxError("orientation 只支持 portrait / landscape。")
            width = section.page_width.cm
            height = section.page_height.cm
            if orientation == "landscape" and width < height:
                section.page_width, section.page_height = Cm(height), Cm(width)
            elif orientation == "portrait" and width > height:
                section.page_width, section.page_height = Cm(height), Cm(width)
            section.orientation = (
                WD_ORIENT.LANDSCAPE if orientation == "landscape" else WD_ORIENT.PORTRAIT
            )
        for attr, value in margins.items():
            setattr(section, attr, Cm(value))


def _set_header_footer(document, operation: dict[str, Any], footer: bool) -> int:
    text = operation.get("text")
    if text is not None and not isinstance(text, str):
        raise DocxError("text 需要字符串。")
    page_number = operation.get("pageNumber") is True
    align = operation.get("align")
    if align is not None and align not in ALIGNMENTS:
        raise DocxError("align 只支持 left / center / right。")
    if not text and not page_number and "text" not in operation:
        raise DocxError("set_header/set_footer 至少需要 text 或 pageNumber:true。")

    sections = _select_sections(document, operation)
    for section in sections:
        region = section.footer if footer else section.header
        region.is_linked_to_previous = False
        paragraphs = region.paragraphs
        first = paragraphs[0]
        for extra in paragraphs[1:]:
            extra._p.getparent().remove(extra._p)
        for run in list(first.runs):
            run._element.getparent().remove(run._element)
        for field in first._p.findall(qn("w:fldSimple")):
            first._p.remove(field)
        if text:
            first.add_run(text)
        if page_number:
            if text:
                first.add_run(" ")
            _add_field(first, " PAGE ", "1")
        if align is not None:
            first.paragraph_format.alignment = ALIGNMENTS[align]
    return len(sections)


def _insert_list(document, originals: list[Paragraph], operation: dict[str, Any]) -> int:
    items = operation.get("items")
    if not isinstance(items, list) or not items:
        raise DocxError("insert_list 需要非空的 items 字符串数组。")
    if not all(isinstance(item, str) for item in items):
        raise DocxError("items 里的每一项都必须是字符串。")
    ordered = operation.get("ordered") is True
    style_name = operation.get("style")
    style = None
    if isinstance(style_name, str) and style_name.strip():
        style = _find_style(document, style_name.strip())
    else:
        for candidate in (("List Number", "ListNumber") if ordered else ("List Bullet", "ListBullet")):
            try:
                style = _find_style(document, candidate)
                break
            except DocxError:
                continue

    paragraphs = [document.add_paragraph() for _ in items]
    after = operation.get("after")
    if after is not None:
        if isinstance(after, bool) or not isinstance(after, int) or after < -1:
            raise DocxError("after 需要整数段落序号；用 -1 表示文首，省略表示文末。")
        if after >= len(originals):
            raise DocxError(f"after {after} 超出范围（共 {len(originals)} 段）。")
        if after == -1:
            if originals:
                for paragraph in reversed(paragraphs):
                    originals[0]._p.addprevious(paragraph._p)
        else:
            cursor = originals[after]._p
            for paragraph in paragraphs:
                cursor.addnext(paragraph._p)
                cursor = paragraph._p

    for index, (paragraph, item) in enumerate(zip(paragraphs, items)):
        if style is not None:
            paragraph.style = style
            paragraph.add_run(item)
        else:
            # 文档没有内置列表样式时退化为带悬挂缩进的符号/编号段落。
            marker = f"{index + 1}." if ordered else "•"
            paragraph.add_run(f"{marker} {item}")
            paragraph.paragraph_format.left_indent = Cm(0.74)
            paragraph.paragraph_format.first_line_indent = Cm(-0.74)
    return len(paragraphs)


def _insert_image(
    document,
    originals: list[Paragraph],
    operation: dict[str, Any],
    workspace: Path | None,
) -> str:
    image = _resolve_image(workspace, str(operation.get("image") or ""))
    after = operation.get("after")
    paragraph = _new_paragraph(document, originals, after)
    run = paragraph.add_run()
    kwargs: dict[str, Any] = {}
    if "widthCm" in operation:
        kwargs["width"] = Cm(_number_value(operation["widthCm"], "widthCm", 0.5, 50))
    if "heightCm" in operation:
        kwargs["height"] = Cm(_number_value(operation["heightCm"], "heightCm", 0.5, 50))
    run.add_picture(str(image), **kwargs)
    align = operation.get("align")
    if align is not None:
        if align not in ALIGNMENTS:
            raise DocxError("align 只支持 left / center / right。")
        paragraph.paragraph_format.alignment = ALIGNMENTS[align]
    caption = operation.get("caption")
    if caption is not None:
        if not isinstance(caption, str):
            raise DocxError("caption 需要字符串。")
        caption_paragraph = document.add_paragraph()
        paragraph._p.addnext(caption_paragraph._p)
        caption_paragraph.add_run(caption)
        caption_paragraph.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        try:
            caption_paragraph.style = _find_style(document, "Caption")
        except DocxError:
            pass
    return image.name


def _insert_hyperlink(document, originals: list[Paragraph], operation: dict[str, Any]) -> None:
    text = operation.get("text")
    url = operation.get("url")
    if not isinstance(text, str) or not text.strip():
        raise DocxError("insert_hyperlink 需要 text。")
    if not isinstance(url, str) or not re.match(r"^(https?://|mailto:)", url.strip(), re.I):
        raise DocxError("url 需要以 http:// / https:// 或 mailto: 开头。")
    paragraph = _new_paragraph(document, originals, operation.get("after"))
    _add_hyperlink(paragraph, text, url.strip())
    align = operation.get("align")
    if align is not None:
        if align not in ALIGNMENTS:
            raise DocxError("align 只支持 left / center / right。")
        paragraph.paragraph_format.alignment = ALIGNMENTS[align]


def _insert_toc(document, originals: list[Paragraph], operation: dict[str, Any]) -> None:
    levels = operation.get("levels", "1-3")
    if not isinstance(levels, str) or not re.match(r"^\d-\d$", levels):
        raise DocxError("levels 需要形如 \"1-3\" 的标题级别范围。")
    after = operation.get("after", -1)
    paragraph = _new_paragraph(document, originals, after)
    _add_field(paragraph, f'TOC \\o "{levels}" \\h \\z \\u', "右键此处选择「更新域」生成目录")


def _insert_page_break(document, originals: list[Paragraph], operation: dict[str, Any]) -> None:
    paragraph = _new_paragraph(document, originals, operation.get("after"))
    paragraph.add_run().add_break(WD_BREAK.PAGE)


def _insert_section_break(document, originals: list[Paragraph], operation: dict[str, Any]) -> None:
    index = operation.get("after")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise DocxError("insert_section_break 需要 after（分节符插在该段之后）。")
    if index >= len(originals):
        raise DocxError(f"after {index} 超出范围（共 {len(originals)} 段）。")
    kind = operation.get("type", "nextPage")
    if kind not in {"nextPage", "continuous"}:
        raise DocxError("type 只支持 nextPage / continuous。")
    paragraph = originals[index]
    p_pr = paragraph._p.get_or_add_pPr()
    sect_pr = p_pr.get_or_add_sectPr()
    if len(sect_pr) == 0:
        source = document.sections[-1]._sectPr
        for child in source:
            sect_pr.append(copy.deepcopy(child))
    sect_type = sect_pr.get_or_add_type()
    sect_type.val = WD_SECTION.NEW_PAGE if kind == "nextPage" else WD_SECTION.CONTINUOUS


def edit_docx(
    path: Path,
    operations: list[dict[str, Any]],
    workspace: Path | None = None,
) -> dict[str, Any]:
    """按操作列表修改 docx 并原地保存，返回改动摘要。"""
    document = _open(path)
    originals = list(document.paragraphs)
    applied: list[dict[str, Any]] = []
    inserts: list[tuple[int, str, str | None]] = []
    deletions: list[int] = []
    appends: list[tuple[str, str | None]] = []
    removed = 0

    if not operations:
        raise DocxError("operations 为空：请提供要执行的操作。")

    for position, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise DocxError(f"第 {position + 1} 个操作不是对象，应形如 {{\"op\": \"replace\", ...}}")
        kind = str(operation.get("op") or "").strip()

        if kind == "replace":
            find = operation.get("find")
            replacement = operation.get("replace")
            if not isinstance(find, str) or not find:
                raise DocxError("replace 需要 find（要查找的文字）且不能为空。")
            if not isinstance(replacement, str):
                raise DocxError("replace 需要 replace（替换后的文字，可为空字符串）。")
            changed = 0
            for paragraph in originals:
                changed += _replace_text(paragraph, find, replacement)
            for table in document.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for paragraph in _cell_paragraphs(cell):
                            changed += _replace_text(paragraph, find, replacement)
            applied.append({"op": kind, "find": find, "occurrences": changed})

        elif kind == "set_paragraph":
            index = _paragraph_index(operation, originals)
            text = operation.get("text")
            if not isinstance(text, str):
                raise DocxError("set_paragraph 需要 text（新的段落文字）。")
            _set_paragraph_text(originals[index], text)
            applied.append({"op": kind, "index": index})

        elif kind == "insert_paragraph":
            after = operation.get("after")
            text = operation.get("text")
            if not isinstance(after, int) or after < -1:
                raise DocxError("insert_paragraph 需要 after（插入到该段落之后；用 -1 表示文首）。")
            if not isinstance(text, str):
                raise DocxError("insert_paragraph 需要 text（新段落文字）。")
            inserts.append((after, text, _optional_style(operation)))
            applied.append({"op": kind, "after": after})

        elif kind == "delete_paragraph":
            index = _paragraph_index(operation, originals)
            deletions.append(index)
            applied.append({"op": kind, "index": index})

        elif kind == "append_paragraph":
            text = operation.get("text")
            if not isinstance(text, str):
                raise DocxError("append_paragraph 需要 text（新段落文字）。")
            appends.append((text, _optional_style(operation)))
            applied.append({"op": kind})

        elif kind == "format_paragraph":
            spec = _format_spec(operation)
            targets = _target_indices(operation, originals)
            for index in targets:
                _apply_run_format(originals[index], spec)
                _apply_paragraph_format(originals[index], spec)
            applied.append(
                {"op": kind, "indices": targets, "fields": sorted(spec)}
            )

        elif kind == "set_table_cell":
            table_index = operation.get("table")
            row_index = operation.get("row")
            column_index = operation.get("column", operation.get("col"))
            text = operation.get("text")
            if not all(isinstance(value, int) for value in (table_index, row_index, column_index)):
                raise DocxError("set_table_cell 需要整数 table / row / column。")
            if not isinstance(text, str):
                raise DocxError("set_table_cell 需要 text（单元格文字）。")
            try:
                table = document.tables[table_index]
                cell = table.cell(row_index, column_index)
            except IndexError as error:
                raise DocxError("table / row / column 超出范围，请先用 docx_read 查看表格结构。") from error
            _set_cell_text(cell, text)
            cell_format = _style_spec(operation, required=False)
            if cell_format:
                for paragraph in cell.paragraphs:
                    _apply_run_format(paragraph, cell_format)
                    _apply_paragraph_format(paragraph, cell_format)
                applied.append(
                    {
                        "op": kind,
                        "table": table_index,
                        "row": row_index,
                        "column": column_index,
                        "fields": sorted(cell_format),
                    }
                )
            else:
                applied.append(
                    {"op": kind, "table": table_index, "row": row_index, "column": column_index}
                )

        elif kind == "insert_table":
            table = _insert_table(document, originals, operation)
            applied.append(
                {
                    "op": kind,
                    "table": _table_display_index(document, table),
                    "rows": len(table.rows),
                    "columns": len(table.columns),
                }
            )

        elif kind == "table_add_row":
            before = operation.get("before")
            _insert_table_row(document, operation)
            applied.append(
                {"op": kind, "table": operation.get("table"), "before": before}
            )

        elif kind == "table_delete_row":
            _delete_table_row(document, operation)
            applied.append(
                {"op": kind, "table": operation.get("table"), "row": operation.get("row")}
            )

        elif kind == "table_add_column":
            before = operation.get("before")
            _insert_table_column(document, operation)
            applied.append(
                {"op": kind, "table": operation.get("table"), "before": before}
            )

        elif kind == "table_delete_column":
            _delete_table_column(document, operation)
            applied.append(
                {
                    "op": kind,
                    "table": operation.get("table"),
                    "column": operation.get("column", operation.get("col")),
                }
            )

        elif kind == "table_merge_cells":
            _merge_table_cells(document, operation)
            applied.append({"op": kind, "table": operation.get("table")})

        elif kind == "table_set":
            table = _table_at(document, operation)
            fields = [key for key in ("style", "align", "widthsPt", "borders", "header") if key in operation]
            if not fields:
                raise DocxError("table_set 至少需要 style / align / widthsPt / borders / header 之一。")
            _apply_table_layout(table, document, operation)
            applied.append({"op": kind, "table": operation.get("table"), "fields": fields})

        elif kind == "delete_table":
            _delete_table(document, operation)
            applied.append({"op": kind, "table": operation.get("table")})

        elif kind == "set_style":
            targets = _apply_style_to_targets(document, operation, originals)
            applied.append(
                {"op": kind, "indices": targets, "style": operation.get("style")}
            )

        elif kind == "set_heading":
            targets = _apply_heading(document, operation, originals)
            applied.append(
                {"op": kind, "indices": targets, "level": operation.get("level")}
            )

        elif kind == "create_style":
            applied.append({"op": kind, "name": _create_style(document, operation)})

        elif kind == "modify_style":
            applied.append({"op": kind, "name": _modify_style(document, operation)})

        elif kind == "set_page":
            sections = _select_sections(document, operation)
            _set_page(document, operation, sections)
            applied.append({"op": kind, "sections": len(sections)})

        elif kind == "set_header":
            count = _set_header_footer(document, operation, footer=False)
            applied.append({"op": kind, "sections": count})

        elif kind == "set_footer":
            count = _set_header_footer(document, operation, footer=True)
            applied.append({"op": kind, "sections": count})

        elif kind == "insert_page_break":
            _insert_page_break(document, originals, operation)
            applied.append({"op": kind, "after": operation.get("after")})

        elif kind == "insert_section_break":
            _insert_section_break(document, originals, operation)
            applied.append(
                {"op": kind, "after": operation.get("after"), "type": operation.get("type", "nextPage")}
            )

        elif kind == "insert_list":
            count = _insert_list(document, originals, operation)
            applied.append({"op": kind, "items": count, "ordered": operation.get("ordered") is True})

        elif kind == "insert_image":
            name = _insert_image(document, originals, operation, workspace)
            applied.append({"op": kind, "image": name, "after": operation.get("after")})

        elif kind == "insert_hyperlink":
            _insert_hyperlink(document, originals, operation)
            applied.append({"op": kind, "after": operation.get("after"), "url": operation.get("url")})

        elif kind == "insert_toc":
            _insert_toc(document, originals, operation)
            applied.append({"op": kind, "levels": operation.get("levels", "1-3")})

        else:
            raise DocxError(
                "未知操作："
                f"{kind or '(空)'}；支持 replace / set_paragraph / insert_paragraph / "
                "delete_paragraph / append_paragraph / format_paragraph / set_style / "
                "set_heading / create_style / modify_style / set_page / set_header / "
                "set_footer / insert_page_break / insert_section_break / insert_table / "
                "table_add_row / table_delete_row / table_add_column / table_delete_column / "
                "table_merge_cells / table_set / delete_table / set_table_cell / "
                "insert_list / insert_image / insert_hyperlink / insert_toc。"
            )

    # 插入：同一个锚点连续插入时，后来的排在前面，保持调用顺序。
    anchors: dict[int, Paragraph] = {}
    for after, text, style in inserts:
        anchor = None if after < 0 else originals[after]
        if after >= 0 and after in anchors:
            anchor = anchors[after]
        new_paragraph = _clone_paragraph(anchor, document)
        _apply_style(new_paragraph, style)
        new_paragraph.add_run(text)
        anchors[after] = new_paragraph

    # 删除：按原始对象移除，与整体序号无关。
    for index in sorted(set(deletions)):
        element = originals[index]._p
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
            removed += 1

    for text, style in appends:
        paragraph = document.add_paragraph()
        _apply_style(paragraph, style)
        paragraph.add_run(text)

    try:
        tmp = path.with_name(f".{path.name}.tmp")
        document.save(str(tmp))
        os.replace(tmp, path)
    except OSError as error:
        raise DocxError(f"保存 docx 失败：{error}") from error

    return {
        "file": {"name": path.name, "size": path.stat().st_size},
        "applied": applied,
        "removedParagraphs": removed,
        "paragraphs": len(document.paragraphs),
        "tables": len(document.tables),
    }


def _paragraph_index(operation: dict[str, Any], paragraphs: list[Paragraph]) -> int:
    index = operation.get("index")
    if not isinstance(index, int):
        raise DocxError("需要整数 index（来自 docx_read 的段落序号）。")
    if index < 0 or index >= len(paragraphs):
        raise DocxError(
            f"段落 index {index} 超出范围（共 {len(paragraphs)} 段），请先用 docx_read 核对。"
        )
    return index


def _optional_style(operation: dict[str, Any]) -> str | None:
    style = operation.get("style")
    return style.strip() if isinstance(style, str) and style.strip() else None

