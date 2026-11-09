"""把结构化提纲渲染成 16:9 的 .pptx。

agent 的汇报技能先产出 slides JSON（标题、要点、图表建议、证据来源），
这里负责排版。只依赖 python-pptx，不启动 Office，也不联网。

slides 形如：

    [{"kind": "title", "title": "...", "subtitle": "..."},
     {"kind": "bullets", "title": "结论式标题",
      "bullets": ["要点一", "要点二"], "chart": "柱状图：ORR 对比",
      "source": "Li 2026, p3", "notes": "讲稿"}]
"""

from __future__ import annotations

import io
import re
from copy import deepcopy
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt
from lxml import etree

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)

FONT = "Microsoft YaHei"
INK = "1A2233"
MUTED = "6B7486"
ACCENT = "2F5FD0"
SOFT = "F4F6FA"
LINE = "E2E6EE"


def _rgb(value: str) -> RGBColor:
    return RGBColor.from_string(value)


def _style(run, size: float, bold: bool = False, color: str = INK) -> None:
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = _rgb(color)
    run.font.name = FONT
    rpr = run._r.get_or_add_rPr()
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = etree.SubElement(rpr, qn("a:ea"))
    ea.set("typeface", FONT)


def _box(slide, left: float, top: float, width: float, height: float):
    frame = slide.shapes.add_textbox(
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    frame.text_frame.word_wrap = True
    return frame.text_frame


def _para(frame, text: str, size: float, bold: bool = False, color: str = INK,
          space_before: float = 0, space_after: float = 0, first: bool = False):
    paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
    paragraph.space_before = Pt(space_before)
    paragraph.space_after = Pt(space_after)
    run = paragraph.add_run()
    run.text = text
    _style(run, size, bold, color)
    return paragraph


def _blank(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])


def _rect(slide, left: float, top: float, width: float, height: float,
          color: str, shape: MSO_SHAPE = MSO_SHAPE.RECTANGLE):
    item = slide.shapes.add_shape(
        shape, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    item.fill.solid()
    item.fill.fore_color.rgb = _rgb(color)
    item.line.fill.background()
    item.shadow.inherit = False
    return item


def _footer(slide, index: int, total: int, source: str = "") -> None:
    line = _rect(slide, 0.75, 6.86, 11.83, 0.012, LINE)
    line.shadow.inherit = False
    frame = _box(slide, 0.75, 6.92, 10.4, 0.4)
    if source:
        _para(frame, source, 10, False, MUTED, first=True)
    _para(
        _box(slide, 11.4, 6.92, 1.2, 0.4),
        f"{index} / {total}",
        10,
        False,
        MUTED,
        first=True,
    )


def _title_slide(prs, slide_data: dict[str, Any], index: int, total: int) -> None:
    slide = _blank(prs)
    _rect(slide, 0.9, 2.05, 0.16, 2.55, ACCENT)
    _rect(slide, 0.9, 5.85, 11.5, 0.012, LINE)

    frame = _box(slide, 1.35, 1.95, 10.6, 2.7)
    _para(frame, str(slide_data.get("title") or "汇报"), 40, True, INK,
          first=True, space_after=10)
    subtitle = str(slide_data.get("subtitle") or "")
    if subtitle:
        _para(frame, subtitle, 17, False, MUTED)

    meta = [str(slide_data.get("author") or ""), str(slide_data.get("date") or "")]
    meta = [item for item in meta if item]
    if meta:
        _para(_box(slide, 1.35, 5.95, 10.6, 0.4), "  ·  ".join(meta), 11,
              False, MUTED, first=True)
    _notes(slide, slide_data)


def _section_slide(prs, slide_data: dict[str, Any], index: int, total: int) -> None:
    slide = _blank(prs)
    _rect(slide, 0, 0, 13.333, 7.5, SOFT)
    _rect(slide, 0.9, 3.1, 0.16, 1.3, ACCENT)
    frame = _box(slide, 1.35, 3.0, 10.6, 1.6)
    _para(frame, str(slide_data.get("title") or ""), 30, True, INK, first=True,
          space_after=6)
    subtitle = str(slide_data.get("subtitle") or "")
    if subtitle:
        _para(frame, subtitle, 15, False, MUTED)
    _notes(slide, slide_data)


def _bullets_slide(prs, slide_data: dict[str, Any], index: int, total: int) -> None:
    slide = _blank(prs)
    chart = str(slide_data.get("chart") or "")
    bullets = [str(item) for item in slide_data.get("bullets") or [] if str(item).strip()]
    width = 7.55 if chart else 11.5

    frame = _box(slide, 0.75, 0.5, width, 1.15)
    _para(frame, str(slide_data.get("title") or ""), 26, True, INK, first=True)
    _rect(slide, 0.78, 1.52, 0.72, 0.045, ACCENT)

    size = 17 if len(bullets) <= 4 else 15 if len(bullets) <= 6 else 13.5
    body = _box(slide, 0.78, 1.85, width, 4.75)
    for position, bullet in enumerate(bullets):
        paragraph = _para(
            body,
            "▪  " + bullet,
            size,
            False,
            INK,
            space_after=12,
            first=position == 0,
        )
        paragraph.line_spacing = 1.28

    if chart:
        card = _rect(slide, 8.75, 1.85, 3.85, 4.55, SOFT, MSO_SHAPE.ROUNDED_RECTANGLE)
        card.adjustments[0] = 0.04
        inner = _box(slide, 9.05, 2.1, 3.25, 4.05)
        _para(inner, "图表建议", 11.5, True, ACCENT, first=True, space_after=10)
        _para(inner, chart, 13, False, INK)

    _footer(slide, index, total, str(slide_data.get("source") or ""))
    _notes(slide, slide_data)


def _notes(slide, slide_data: dict[str, Any]) -> None:
    text = str(slide_data.get("notes") or "").strip()
    if text:
        slide.notes_slide.notes_text_frame.text = text


def build_deck(
    slides: list[dict[str, Any]],
    title: str = "",
    template: bytes | None = None,
) -> bytes:
    """渲染 PPT；给了 ``template``（用户上传的 .pptx）就在模板基础上制作。

    模板支持两种用法，按优先级自动判断：

    1. **占位符页**：模板页里写 ``{{title}}`` / ``{{subtitle}}`` / ``{{bullets}}``
       / ``{{chart}}`` / ``{{source}}`` / ``{{notes}}``，直接复制该页并替换内容，
       版式、配色、装饰元素完全保留；
    2. **母版版式**：没有占位符时，按模板的幻灯片版式（封面 / 章节 / 标题和内容）
       新建页面，继承主题字体、配色和背景；模板里自带的示例页会被移除。
    """
    items = [item for item in slides if isinstance(item, dict)]
    if not items:
        items = [{"kind": "title", "title": title or "汇报"}]

    if template:
        return _build_from_template(template, items, title)

    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H

    total = len(items)
    for index, item in enumerate(items, start=1):
        kind = _kind_of(item, index)
        if kind == "title":
            _title_slide(prs, item, index, total)
        elif kind == "section":
            _section_slide(prs, item, index, total)
        else:
            _bullets_slide(prs, item, index, total)

    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 用户模板
# ---------------------------------------------------------------------------

_TOKEN_NAMES = ("title", "subtitle", "bullets", "chart", "source", "author", "date", "notes")
_TOKEN = re.compile(r"\{\{\s*(" + "|".join(_TOKEN_NAMES) + r")\s*\}\}")


def _kind_of(item: dict[str, Any], index: int) -> str:
    kind = str(item.get("kind") or "").lower()
    if not kind:
        kind = "title" if index == 1 and item.get("subtitle") else "bullets"
    if kind in {"section", "divider"}:
        return "section"
    return "title" if kind == "title" else "bullets"


def _walk_shapes(shapes):
    for shape in shapes:
        yield shape
        if hasattr(shape, "shapes"):
            yield from _walk_shapes(shape.shapes)


def _slide_text(slide) -> str:
    chunks = [
        shape.text_frame.text
        for shape in _walk_shapes(slide.shapes)
        if getattr(shape, "has_text_frame", False)
    ]
    return "\n".join(chunks)


def _run_props(paragraph_el):
    run = paragraph_el.find(qn("a:r"))
    if run is None:
        return None
    props = run.find(qn("a:rPr"))
    return deepcopy(props) if props is not None else None


def _rewrite_paragraph(paragraph_el, lines: list[str]) -> None:
    """保留段落属性（项目符号、缩进）和首个 run 的字体，按行重建正文。"""
    ppr = paragraph_el.find(qn("a:pPr"))
    rpr = _run_props(paragraph_el)
    for child in list(paragraph_el):
        if child is not ppr:
            paragraph_el.remove(child)
    blank = deepcopy(paragraph_el)

    last = paragraph_el
    for position, line in enumerate(lines or [""]):
        if position:
            clone = deepcopy(blank)
            last.addnext(clone)
            last = clone
        run = etree.SubElement(last, qn("a:r"))
        if rpr is not None:
            run.append(deepcopy(rpr))
        text = etree.SubElement(run, qn("a:t"))
        text.text = line


def _set_frame_lines(text_frame, lines: list[str]) -> None:
    paragraphs = text_frame.paragraphs
    _rewrite_paragraph(paragraphs[0]._p, lines)
    for paragraph in paragraphs[1:]:
        paragraph._p.getparent().remove(paragraph._p)


def _replace_tokens(slide, item: dict[str, Any]) -> None:
    bullets = [str(value) for value in item.get("bullets") or [] if str(value).strip()]
    chart = str(item.get("chart") or "").strip()
    if chart:
        bullets = bullets + [f"图表：{chart}"]

    mapping = {
        "title": str(item.get("title") or ""),
        "subtitle": str(item.get("subtitle") or ""),
        "chart": chart,
        "source": str(item.get("source") or ""),
        "author": str(item.get("author") or ""),
        "date": str(item.get("date") or ""),
        "notes": str(item.get("notes") or ""),
        "bullets": "",
    }

    for shape in _walk_shapes(slide.shapes):
        if not getattr(shape, "has_text_frame", False):
            continue
        frame = shape.text_frame
        for paragraph in list(frame.paragraphs):
            original = "".join(run.text for run in paragraph.runs) or paragraph.text
            if "{{bullets}}" in original:
                prefix, suffix = original.split("{{bullets}}", 1)
                lines = [f"{prefix}{value}{suffix}".strip() for value in bullets] or [
                    prefix.strip()
                ]
                _rewrite_paragraph(paragraph._p, lines)
                continue
            if not _TOKEN.search(original):
                continue
            replaced = _TOKEN.sub(lambda match: mapping.get(match.group(1), ""), original)
            runs = paragraph.runs
            if runs:
                runs[0].text = replaced
                for run in runs[1:]:
                    run._r.getparent().remove(run._r)


def _clone_slide(prs, source):
    """复制一页幻灯片（含图片等关系），用于复用用户模板里的设计页。"""
    slide = prs.slides.add_slide(source.slide_layout)
    for shape in list(slide.shapes):
        shape._element.getparent().remove(shape._element)

    id_map: dict[str, str] = {}
    for rel in source.part.rels.values():
        if rel.reltype in {RT.SLIDE_LAYOUT, RT.NOTES_SLIDE}:
            continue
        if rel.is_external:
            new_id = slide.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:
            new_id = slide.part.relate_to(rel.target_part, rel.reltype)
        id_map[rel.rId] = new_id

    for shape in source.shapes:
        element = deepcopy(shape._element)
        for node in element.iter():
            for attribute in ("r:embed", "r:id", "r:link"):
                key = qn(attribute)
                rel_id = node.get(key)
                if rel_id and rel_id in id_map:
                    node.set(key, id_map[rel_id])
        slide.shapes._spTree.append(element)

    _copy_background(source, slide)
    return slide


def _copy_background(source, target) -> None:
    background = source._element.cSld.bg
    if background is None:
        return
    existing = target._element.cSld.bg
    if existing is not None:
        target._element.cSld.remove(existing)
    target._element.cSld.insert(0, deepcopy(background))


def _delete_slides(prs, slide_ids: set[int]) -> None:
    sld_id_list = prs.slides._sldIdLst
    for sld_id in list(sld_id_list):
        if int(sld_id.get("id") or 0) in slide_ids:
            prs.part.drop_rel(sld_id.rId)
            sld_id_list.remove(sld_id)


def _placeholder_types(layout) -> set[Any]:
    values: set[Any] = set()
    for placeholder in layout.placeholders:
        try:
            values.add(placeholder.placeholder_format.type)
        except (AttributeError, KeyError):
            continue
    return values


def _pick_layout(prs, kind: str):
    layouts = list(prs.slide_layouts)
    if not layouts:
        return None
    if kind == "title":
        for layout in layouts:
            if PP_PLACEHOLDER.SUBTITLE in _placeholder_types(layout):
                return layout
    elif kind == "section":
        for layout in layouts:
            if PP_PLACEHOLDER.CENTER_TITLE in _placeholder_types(layout):
                return layout
    else:
        for layout in layouts:
            types = _placeholder_types(layout)
            if PP_PLACEHOLDER.BODY in types or PP_PLACEHOLDER.OBJECT in types:
                return layout
        for layout in layouts:
            if PP_PLACEHOLDER.TITLE in _placeholder_types(layout):
                return layout
    return layouts[0]


def _fill_layout_slide(prs, slide, item: dict[str, Any]) -> None:
    title_frame = slide.shapes.title
    title_text = str(item.get("title") or "")
    if title_frame is not None:
        title_frame.text = title_text

    bullets = [str(value) for value in item.get("bullets") or [] if str(value).strip()]
    chart = str(item.get("chart") or "").strip()
    if chart:
        bullets = bullets + [f"图表：{chart}"]

    body = None
    for placeholder in slide.placeholders:
        try:
            kind = placeholder.placeholder_format.type
        except (AttributeError, KeyError):
            continue
        if kind == PP_PLACEHOLDER.SUBTITLE:
            placeholder.text_frame.text = str(item.get("subtitle") or "")
        elif kind in {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT}:
            body = placeholder

    if bullets:
        if body is not None:
            _set_frame_lines(body.text_frame, bullets)
        else:
            width = prs.slide_width - Inches(1.8)
            height = prs.slide_height - Inches(3.0)
            frame = slide.shapes.add_textbox(Inches(0.9), Inches(1.9), width, height)
            frame.text_frame.word_wrap = True
            _set_frame_lines(frame.text_frame, bullets)

    available = slide.notes_slide.notes_text_frame
    notes = str(item.get("notes") or "").strip()
    source = str(item.get("source") or "").strip()
    if source:
        notes = f"{notes}\n来源：{source}".strip()
    if notes:
        available.text = notes


def _build_from_template(template: bytes, items: list[dict[str, Any]], title: str) -> bytes:
    prs = Presentation(io.BytesIO(template))
    if items and title and not str(items[0].get("title") or "").strip():
        items[0]["title"] = title
    template_slides = list(prs.slides)
    original_ids = {int(slide.slide_id) for slide in template_slides}

    token_slides: dict[str, Any] = {}
    for slide in template_slides:
        text = _slide_text(slide)
        if "{{bullets}}" in text:
            token_slides.setdefault("bullets", slide)
        elif "{{subtitle}}" in text:
            token_slides.setdefault("title", slide)
        elif "{{title}}" in text:
            token_slides.setdefault("section", slide)

    if token_slides:
        fallback = token_slides.get("bullets") or token_slides.get("title") or template_slides[0]
        for index, item in enumerate(items, start=1):
            kind = _kind_of(item, index)
            source = token_slides.get(kind) or fallback
            slide = _clone_slide(prs, source)
            _replace_tokens(slide, item)
            _notes(slide, item)
    else:
        cover_sample = template_slides[0] if template_slides else None
        body_sample = next(
            (
                slide
                for slide in template_slides[1:]
                if PP_PLACEHOLDER.BODY in _placeholder_types(slide.slide_layout)
                or PP_PLACEHOLDER.OBJECT in _placeholder_types(slide.slide_layout)
            ),
            cover_sample,
        )
        for index, item in enumerate(items, start=1):
            kind = _kind_of(item, index)
            layout = _pick_layout(prs, kind) or prs.slide_layouts[0]
            slide = prs.slides.add_slide(layout)
            sample = cover_sample if kind == "title" else body_sample
            if sample is not None:
                _copy_background(sample, slide)
            _fill_layout_slide(prs, slide, item)

    _delete_slides(prs, original_ids)
    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


def parse_outline(markdown: str, title: str = "") -> list[dict[str, Any]]:
    """把 Markdown 提纲退化成 slides；没有结构化 JSON 时仍然能出 PPT。"""
    slides: list[dict[str, Any]] = []
    deck_title = title.strip()
    current: dict[str, Any] | None = None

    for raw in markdown.splitlines():
        line = raw.rstrip()
        heading = _HEADING.match(line.strip())
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            if level == 1 and not deck_title:
                deck_title = text
                continue
            if level <= 2:
                current = {"kind": "bullets", "title": text, "bullets": []}
                slides.append(current)
                continue
        stripped = line.strip()
        if current is None:
            continue
        if stripped.startswith(("- ", "* ")):
            current["bullets"].append(stripped[2:].strip())
        elif stripped.startswith("来源"):
            current["source"] = stripped.split("：", 1)[-1].strip()
        elif stripped.startswith("图表"):
            current["chart"] = stripped.split("：", 1)[-1].strip()
        elif stripped and not current.get("subtitle"):
            current["subtitle"] = stripped

    if deck_title:
        slides.insert(
            0,
            {"kind": "title", "title": deck_title, "subtitle": "组会汇报"},
        )
    for item in slides:
        if not item.get("bullets"):
            item.pop("bullets", None)
            item["kind"] = item.get("kind", "section")
    return slides
