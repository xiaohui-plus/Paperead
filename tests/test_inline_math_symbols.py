from paperead.inline_math import (
    build_inline_math,
    build_symbol_fallbacks,
    spans_with_offsets,
)
from paperead.layout import Block
from paperead.vision_text import _candidate, _restore


def _span(text, font, x0, x1, size=8.0):
    return {
        "text": text,
        "font": font,
        "size": size,
        "bbox": (x0, 100.0, x1, 108.0),
        "origin": (x0, 107.0),
    }


def test_stix_private_use_glyph_becomes_fallback_crop():
    """STIX 花体 U 以 PUA 码点出现：不能猜字母，整组标记裁图。"""
    spans = [
        _span("\ue235", "STIXMathCalligraphy-Regular", 100, 106),
        _span("= {", "STIXMath-Regular", 106, 118),
    ]
    text, ranges = spans_with_offsets(spans)
    out = build_inline_math(spans, ranges)
    assert len(out) == 1
    assert out[0].fallback is True
    assert out[0].latex == ""
    assert out[0].bbox == (100.0, 100.0, 118.0, 108.0)
    assert out[0].source == text


def test_isolated_private_use_glyph_gets_its_own_crop_interval():
    """公式组之外的孤立 PUA 也要补兜底区间，否则正文永久显示方块。"""
    spans = [
        _span("Let ", "CharisSIL", 50, 66, size=10.0),
        _span("\ue235", "CharisSIL", 66, 72),
        _span(" be the set", "CharisSIL", 72, 120, size=10.0),
    ]
    text, ranges = spans_with_offsets(spans)
    assert build_inline_math(spans, ranges) == []
    out = build_symbol_fallbacks(text, ranges)
    assert len(out) == 1
    assert (out[0].start, out[0].end) == (4, 5)
    assert out[0].fallback is True
    assert out[0].bbox == (66.0, 100.0, 72.0, 108.0)


def test_unicode_math_italic_is_normalized_to_latex():
    """𝑖/𝑁 是 Unicode 数学字母，要归一化成 i/N 而不是原样塞进 LaTeX。"""
    spans = [_span("𝑖", "STIXMath-Italic", 100, 104)]
    text, ranges = spans_with_offsets(spans)
    out = build_inline_math(spans, ranges)
    assert len(out) == 1
    assert out[0].latex == "i"
    assert out[0].fallback is False


def test_symbol_fallback_is_skipped_when_math_group_already_covers_it():
    spans = [
        _span("\ue235", "STIXMathCalligraphy-Regular", 100, 106),
        _span("= {", "STIXMath-Regular", 106, 118),
    ]
    text, ranges = spans_with_offsets(spans)
    existing = [m.to_dict() for m in build_inline_math(spans, ranges)]
    assert build_symbol_fallbacks(text, ranges, existing) == []


def test_single_private_use_glyph_makes_paragraph_a_vision_candidate():
    b = Block(
        id="p", order=0, page=1, kind="para",
        text="Let \ue235 be the set of users and items in the graph.",
        bbox=(40, 45, 400, 85), inline_math=[],
    )
    assert _candidate(b) is True


def test_vision_restore_accepts_text_only_fix_for_abnormal_symbol_paragraph():
    """只有异常符号的段落，模型可以只返回纠正后的纯文本。"""
    original = "Let \ue235 be the set of users."
    text, spans = _restore(
        '{"text": "Let U be the set of users.", "formulas": []}', original
    )
    assert text == "Let U be the set of users."
    assert spans == []


def test_vision_restore_still_rejects_plain_paragraph_without_formulas():
    original = "The condition number k controls convergence."
    try:
        _restore('{"text": "The method.", "formulas": []}', original)
    except ValueError:
        return
    raise AssertionError("plain formula paragraph must not accept empty formulas")