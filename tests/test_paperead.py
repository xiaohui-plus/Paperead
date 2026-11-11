"""版面重排与翻译的单元测试（不依赖真实 PDF）。

覆盖几个踩过的坑：
- span 拼接丢空格
- 行内公式把段落切碎 / 被误判成公式
- 续段判定被 "et al." 这类缩写骗过
- 占位符还原把正文里的 `[2]` 当成占位符，导致引用编号整体错位
"""

from paperead.layout import (
    Line,
    _continues_paragraph,
    _ends_sentence,
    _join_spans,
    _looks_equation,
    _merge_visual_rows,
    _table_crop_rect,
)
from paperead.translate import protect, restore


# ── span 拼接 ──────────────────────────────────────────────────────────

def test_join_spans_inserts_space_by_gap():
    spans = [
        {"text": "central", "bbox": [0, 0, 30, 10], "size": 10},
        {"text": "node", "bbox": [33, 0, 55, 10], "size": 10},
    ]
    assert _join_spans(spans) == "central node"


def test_join_spans_keeps_kerning_tight():
    spans = [
        {"text": "learn", "bbox": [0, 0, 25, 10], "size": 10},
        {"text": "ing", "bbox": [25.1, 0, 34, 10], "size": 10},
    ]
    assert _join_spans(spans) == "learning"


# ── 公式判定 ───────────────────────────────────────────────────────────

def test_inline_math_sentence_is_not_equation():
    text = "Where A := D-1/2 A D-1/2 denotes the normalized adjacency matrix."
    assert _looks_equation(text, is_math_font=False) is False


def test_display_equation_is_equation():
    assert _looks_equation("H(l+1) = sigma(A H(l) W(l)).", is_math_font=True) is True


def test_pua_delimiter_line_is_equation():
    """分段函数的大括号是 CMEX 私用区字形，整行必须当公式，否则会渲染成豆腐块并送去翻译。"""
    text = "\uf8f21 + min(dx, dy) + (d/2)[(d/2)"
    assert _looks_equation(text, is_math_font=False) is True


def test_pua_bullet_with_long_text_is_not_equation():
    """Word 导出的项目符号也是私用区字符，但后面跟着长句，不能误判成公式。"""
    text = "\uf0b7 This section describes the experimental setup and the evaluation protocol."
    assert _looks_equation(text, is_math_font=False) is False


# ── 续段判定 ───────────────────────────────────────────────────────────

def test_ends_sentence_ignores_abbreviation():
    assert _ends_sentence("results from Kipf et al.") is False
    assert _ends_sentence("the adjacency matrix.") is True
    assert _ends_sentence("see Fig.") is False


def test_continues_paragraph_does_not_swallow_caption():
    """右栏首行是图注时，不能被上一栏停在逗号/连字符的正文续段吞并。"""
    prev = "indicating the presence of 4 common citations who are not widely cited,"
    assert _continues_paragraph(prev, "Figure 3: Time consumption of SIEG.") is False
    assert _continues_paragraph(prev, "which should be an important reason.") is True


# ── 表格裁图范围 ───────────────────────────────────────────────────────

def test_table_crop_ignores_text_in_other_column():
    """双栏页的表格裁图只扩到本栏，不能把另一栏正文裁进来。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((54, 285), "left column body text that must stay out")
    page.insert_text((340, 280), "Model Hits MRR")
    rect = pymupdf.Rect(335, 260, 542, 306)

    crop = _table_crop_rect(page, rect)

    assert crop.x0 > 300      # 左栏文字（x0=54）没有被吸进来
    assert crop.x1 <= 560


# ── 视觉行合并 ─────────────────────────────────────────────────────────

def _line(text, x0, x1, baseline, size=10.0):
    return Line(
        text=text, bbox=(x0, baseline - size, x1, baseline + 2),
        page=1, size=size, baseline=baseline,
    )


def test_merge_visual_rows_joins_inline_math_fragment():
    lines = [
        _line("Where", 54, 80, 620),
        _line("A := D", 82, 130, 620),          # 同一基线的行内公式
        _line("denotes the matrix.", 132, 240, 620),
        _line("Next paragraph line.", 54, 240, 634),
    ]
    rows = _merge_visual_rows(lines)
    assert len(rows) == 2
    assert "Where A := D denotes the matrix." in rows[0].text
    assert rows[1].text == "Next paragraph line."


def test_merge_visual_rows_does_not_join_different_lines():
    lines = [
        _line("first line of text", 54, 240, 620),
        _line("second line of text", 54, 240, 634),
    ]
    assert len(_merge_visual_rows(lines)) == 2


def test_merge_visual_rows_keeps_superscript_on_same_row():
    """上标基线上移 ~3pt，仍应并入同一行。"""
    lines = [
        _line("energy", 54, 100, 620),
        Line(text="2", bbox=(101, 610, 108, 617), page=1, size=6.0, baseline=617),
    ]
    rows = _merge_visual_rows(lines)
    assert len(rows) == 1


# ── 占位符 ─────────────────────────────────────────────────────────────

def test_protect_restore_roundtrip():
    src = "processes [1] and [2], see Fig. 3 and $x=1$."
    masked, pieces = protect(src)
    assert "[1]" not in masked
    assert "⟦P0⟧" in masked
    assert restore(masked, pieces) == src


def test_restore_does_not_rewrite_literal_brackets():
    """模型自己写的 [2] 不能被当成占位符替换，否则引用编号会整体错位。"""
    masked, pieces = protect("results [1] and [2]")
    out = restore("结果 ⟦P0⟧ 以及 [2]", pieces)
    assert out == "结果 [1] 以及 [2]"


def test_protect_keeps_gene_symbol():
    masked, pieces = protect("OsDREB1A is induced by drought.")
    assert "OsDREB1A" not in masked
    assert restore(masked, pieces) == "OsDREB1A is induced by drought."


# ── 图注续行合并 ───────────────────────────────────────────────────────

def test_merge_caption_continuations_joins_tight_lines():
    from paperead.layout import Block, _merge_caption_continuations

    cap = Block(
        id="b1", order=0, page=8, kind="caption",
        text="Figure 3: Baseline performance", bbox=(108, 300, 504, 312),
    )
    cont = Block(
        id="b2", order=1, page=8, kind="para",
        text="on SELECTBENCH-Case.", bbox=(108, 313, 504, 324),
    )
    body = Block(
        id="b3", order=2, page=8, kind="para",
        text="Body paragraph far below.", bbox=(108, 400, 504, 420),
    )

    out = _merge_caption_continuations([cap, cont, body])

    assert len(out) == 2
    assert out[0].kind == "caption"
    assert "SELECTBENCH-Case" in out[0].text
    assert out[1].id == "b3"
