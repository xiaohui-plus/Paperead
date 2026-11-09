"""PPT 导出测试：内置版式与用户模板（占位符页 / 母版版式）。"""

from __future__ import annotations

import base64
import io

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Inches

from server.research.deck import build_deck

WIDE_W = Inches(13.333)
WIDE_H = Inches(7.5)
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _deck_text(prs) -> str:
    chunks: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                chunks.append(shape.text_frame.text)
    return "\n".join(chunks)


def _template_with_tokens() -> bytes:
    prs = Presentation()
    prs.slide_width = WIDE_W
    prs.slide_height = WIDE_H

    cover = prs.slides.add_slide(prs.slide_layouts[0])
    cover.shapes.title.text = "{{title}}"
    cover.placeholders[1].text = "{{subtitle}}"
    cover.shapes.add_picture(io.BytesIO(PNG_1PX), Inches(0.2), Inches(0.2), Inches(0.3), Inches(0.3))

    content = prs.slides.add_slide(prs.slide_layouts[1])
    content.shapes.title.text = "{{title}}"
    content.placeholders[1].text_frame.text = "{{bullets}}"

    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


def _template_without_tokens() -> bytes:
    prs = Presentation()
    prs.slide_width = WIDE_W
    prs.slide_height = WIDE_H
    sample = prs.slides.add_slide(prs.slide_layouts[0])
    sample.shapes.title.text = "示例封面"
    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


def test_template_token_pages_are_cloned_and_filled() -> None:
    slides = [
        {"kind": "title", "title": "真实封面", "subtitle": "组会汇报"},
        {"kind": "bullets", "title": "三条耐药通路", "bullets": ["IFN-γ 失活", "T 细胞耗竭"],
         "source": "Li 2026, p3", "notes": "先讲结论。"},
        {"kind": "bullets", "title": "下一步验证", "bullets": ["补体外实验"]},
    ]
    blob = build_deck(slides, template=_template_with_tokens())
    prs = Presentation(io.BytesIO(blob))

    assert len(prs.slides) == 3
    text = _deck_text(prs)
    assert "真实封面" in text and "组会汇报" in text
    assert "三条耐药通路" in text
    assert "IFN-γ 失活" in text and "T 细胞耗竭" in text
    assert "{{" not in text
    assert "示例封面" not in text
    assert prs.slides[1].notes_slide.notes_text_frame.text == "先讲结论。"
    assert prs.slide_width == WIDE_W and prs.slide_height == WIDE_H
    pictures = [
        shape
        for shape in prs.slides[0].shapes
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE
    ]
    assert pictures, "模板封面上的图片应随克隆保留"


def test_template_layouts_replace_sample_slides() -> None:
    slides = [
        {"kind": "title", "title": "真实封面", "subtitle": "副标题"},
        {"kind": "bullets", "title": "结论式标题", "bullets": ["要点一", "要点二"]},
    ]
    blob = build_deck(slides, template=_template_without_tokens())
    prs = Presentation(io.BytesIO(blob))

    assert len(prs.slides) == 2
    text = _deck_text(prs)
    assert "真实封面" in text and "副标题" in text
    assert "结论式标题" in text and "要点一" in text and "要点二" in text
    assert "示例封面" not in text
    assert prs.slide_width == WIDE_W and prs.slide_height == WIDE_H


def test_builtin_deck_is_16_by_9() -> None:
    blob = build_deck([{"kind": "title", "title": "默认汇报"}])
    prs = Presentation(io.BytesIO(blob))
    assert prs.slide_width == Inches(13.333)
    assert prs.slide_height == Inches(7.5)
    assert "默认汇报" in _deck_text(prs)
