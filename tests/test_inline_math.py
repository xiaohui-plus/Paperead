"""正文行内公式重建的单元测试（不依赖外部 PDF）。"""

from __future__ import annotations

from paperead.inline_math import (
    build_inline_math,
    spans_with_offsets,
    tex_brace_delta,
    tex_braces_balanced,
)
from paperead.layout import _merge_cross_row_math, _merge_visual_lines


def _span(text: str, font: str, size: float, x: float, y: float, width: float) -> dict:
    return {
        "text": text,
        "font": font,
        "size": size,
        "bbox": (x, y - size * 0.75, x + width, y + size * 0.25),
        "origin": (x, y),
        "flags": 0,
    }


def test_rebuild_superscript_and_subscript():
    spans = [
        _span("Here, ", "NimbusRomNo9L-Regu", 10, 50, 100, 30),
        _span("P", "CMSY10", 10, 80, 100, 7),
        _span("query", "CMMI7", 7, 87, 95.5, 17),
        _span("SPD(", "CMR7", 7, 86, 103.5, 14),
        _span("i,j", "CMMI7", 7, 100, 103.5, 8),
        _span(")", "CMR7", 7, 108, 103.5, 4),
        _span(" denote", "NimbusRomNo9L-Regu", 10, 112, 100, 30),
    ]
    text, ranges = spans_with_offsets(spans)
    math = build_inline_math(spans, ranges)

    assert len(math) == 1
    assert text[math[0].start:math[0].end] == "PquerySPD(i,j)"
    assert math[0].latex == r"\mathcal{P}^{query}_{\mathrm{SPD}(i,j)}"
    assert math[0].fallback is False


def test_percent_and_dots_are_escaped():
    spans = [
        _span("d", "CMMI10", 10, 50, 100, 5),
        _span("%2", "CMR10", 10, 55, 100, 10),
        _span("· · ·", "CMSY10", 10, 70, 100, 15),
    ]
    text, ranges = spans_with_offsets(spans)
    math = build_inline_math(spans, ranges)

    assert len(math) == 1
    assert r"\%" in math[0].latex
    assert r"\cdots" in math[0].latex
    assert r"\cdot\cdot" not in math[0].latex


def test_hard_operator_falls_back_for_cropping():
    spans = [
        _span("y", "CMMI10", 10, 50, 100, 5),
        _span("=", "CMR10", 10, 56, 100, 6),
        _span("√x", "CMEX10", 10, 64, 100, 12),
    ]
    text, ranges = spans_with_offsets(spans)
    math = build_inline_math(spans, ranges)

    assert len(math) == 1
    assert math[0].fallback is True
    assert math[0].bbox is not None


def test_two_column_visual_rows_stay_separate():
    lines = [
        {
            "spans": [_span("left", "NimbusRomNo9L-Regu", 10, 54, 100, 24)],
            "bbox": (54, 90, 78, 110),
            "dir": (1, 0),
        },
        {
            "spans": [_span("right", "NimbusRomNo9L-Regu", 10, 330, 100, 28)],
            "bbox": (330, 90, 358, 110),
            "dir": (1, 0),
        },
    ]
    merged = _merge_visual_lines(lines)
    assert len(merged) == 2
    assert [line["spans"][0]["text"] for line in merged] == ["left", "right"]

def test_cmr_prose_line_does_not_become_inline_math():
    """AASTeX 的正文整行就是 CMR10；CMR 只能并入已开启的公式组。"""
    spans = [_span("We present a spatially resolved study of the superwind", "CMR10", 10, 50, 100, 260)]
    text, ranges = spans_with_offsets(spans)

    assert text.startswith("We present")
    assert build_inline_math(spans, ranges) == []


def test_cmr_prose_keeps_only_the_formula_fragment():
    spans = [
        _span("the nearby (", "CMR10", 10, 50, 100, 60),
        _span("d", "CMMI10", 10, 110, 100, 6),
        _span(" ∼ ", "CMSY10", 10, 116, 100, 12),
        _span("3.5", "CMR10", 10, 128, 100, 15),
        _span(" Mpc", "CMR10", 10, 143, 100, 25),
        _span(") galaxy", "CMR10", 10, 168, 100, 45),
    ]
    text, ranges = spans_with_offsets(spans)
    math = build_inline_math(spans, ranges)

    assert len(math) == 1
    fragment = text[math[0].start:math[0].end]
    assert "d" in fragment and "3.5" in fragment
    assert "nearby" not in fragment
    assert "galaxy" not in fragment

def test_braces_are_escaped_as_visible_glyphs():
    """正文里的花括号必须转义，否则 KaTeX 会把它们当分组符吞掉。"""
    spans = [
        _span("{", "CMMI10", 10, 50, 100, 5),
        _span("2", "CMR10", 10, 55, 100, 5),
        _span("}", "CMMI10", 10, 60, 100, 5),
    ]
    text, ranges = spans_with_offsets(spans)
    math = build_inline_math(spans, ranges)

    assert len(math) == 1
    assert text[math[0].start:math[0].end] == "{2}"
    assert r"\{" in math[0].latex
    assert r"\}" in math[0].latex


def test_brace_balance_ignores_escaped_braces():
    assert tex_brace_delta("{2 ×") == 1
    assert tex_brace_delta("10−3, 1 × 10−2}") == -1
    assert tex_brace_delta("{10−3, 10−4}") == 0
    assert tex_braces_balanced(r"\{2\times") is True
    assert tex_braces_balanced(r"10^{-2}\}") is True
    assert tex_braces_balanced("}{") is False


def test_cross_row_math_fragments_are_merged():
    """跨行切断的集合公式应拼回一条完整 LaTeX。"""
    text = "{2 × 10−3, 4 × 10−3}"
    inline_math = [
        {
            "start": 0, "end": 4, "latex": r"\{2\times",
            "fallback": False, "source": "{2 ×", "bbox": [543, 217, 558, 226],
        },
        {
            "start": 5, "end": len(text), "latex": r"10^{-3},4\times10^{-3}\}",
            "fallback": False, "source": "10−3, 4 × 10−3}",
            "bbox": [307, 226, 452, 236],
        },
    ]
    merged = _merge_cross_row_math(text, inline_math)

    assert len(merged) == 1
    assert merged[0]["start"] == 0
    assert merged[0]["end"] == len(text)
    assert merged[0]["latex"] == r"\{2\times10^{-3},4\times10^{-3}\}"
    assert merged[0]["fallback"] is False


def test_cross_row_merge_stops_at_prose():
    """中间夹了正文就不能合并：两条各自降级裁图，绝不产出半截公式。"""
    text = "{2 × and 10−3}"
    inline_math = [
        {
            "start": 0, "end": 4, "latex": r"\{2\times",
            "fallback": False, "source": "{2 ×", "bbox": [543, 217, 558, 226],
        },
        {
            "start": 9, "end": 14, "latex": r"10^{-3}\}",
            "fallback": False, "source": "10−3}", "bbox": [307, 226, 340, 236],
        },
    ]
    merged = _merge_cross_row_math(text, inline_math)

    assert len(merged) == 2
    assert all(item["fallback"] for item in merged)
    assert all(item["latex"] == "" for item in merged)


def test_balanced_formulas_are_not_merged():
    """两条各自完整的公式不能因为相邻就被误拼。"""
    text = "10^{-4},10^{-6}"
    inline_math = [
        {"start": 0, "end": 6, "latex": "10^{-4}", "fallback": False, "source": "10−4"},
        {"start": 7, "end": 13, "latex": "10^{-6}", "fallback": False, "source": "10−6"},
    ]

    assert _merge_cross_row_math(text, inline_math) == inline_math