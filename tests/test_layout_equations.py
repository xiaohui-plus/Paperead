"""公式编号识别的回归测试。

对应问题：双栏 PDF 左栏公式的编号贴在**栏**右缘（页面宽度 48% 左右），
旧判据按整页宽度的 60% 判断「编号靠右」，导致 (9)(10)(11) 这类编号
在页眉页脚去重阶段就被整批删除；另外行内拼接会把编号插进公式正文中间。
"""

from __future__ import annotations

import pymupdf

from paperead.layout import (
    Block,
    _aligns_column_edge,
    _attach_equation_numbers,
    _split_eq_number,
)


# ── 编号拆分 ────────────────────────────────────────────────────────────

def test_split_eq_number_at_line_end():
    body, number = _split_eq_number("aij = qi · k⊤ j + bij (9)")
    assert number == "9"
    assert "(9)" not in body


def test_split_eq_number_in_formula_middle():
    """视觉行按 x 拼接后编号可能落在上下标之间。"""
    body, number = _split_eq_number("aij = qi · k⊤ √ (9) j + bij d")
    assert number == "9"
    assert "(9)" not in body


def test_split_eq_number_keeps_matrix_power():
    """`W(2)` 是矩阵上标，不是公式编号，不能剥掉。"""
    text = "H = ReLU(AXW(1) a )W(2) a )"
    body, number = _split_eq_number(text)
    assert number == ""
    assert body == text


# ── 栏右缘判定 ──────────────────────────────────────────────────────────

def test_aligns_column_edge():
    edges = [96.0, 292.5, 558.0]
    assert _aligns_column_edge(edges, 292.5)
    assert _aligns_column_edge(edges, 289.0)
    assert not _aligns_column_edge(edges, 250.0)


# ── 编号挂载 ────────────────────────────────────────────────────────────

def _doc_one_page() -> pymupdf.Document:
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    return doc


def _equation_block(text, bbox, number="") -> Block:
    return Block(
        id="", order=0, page=1, kind="equation", text=text, bbox=bbox,
        translatable=False, eq_number=number,
    )


def _para_block(text, bbox) -> Block:
    return Block(id="", order=0, page=1, kind="para", text=text, bbox=bbox)


def test_left_column_number_attaches_to_equation():
    """双栏左栏编号 x1≈292（< 612*0.6）也要挂到公式上。"""
    doc = _doc_one_page()
    blocks = [
        _para_block("Right column body text that reaches the column edge.", (320, 300, 558, 312)),
        _para_block("Left column body text that fills the whole column width.", (54, 320, 292.5, 332)),
        _equation_block("aij = qi · k⊤ j + bij", (124, 279, 200, 307)),
        _para_block("(9)", (278, 289, 292.5, 299)),
    ]
    out = _attach_equation_numbers(blocks, doc)
    eq = [b for b in out if b.kind == "equation"]
    assert len(eq) == 1
    assert eq[0].eq_number == "9"
    assert not any(b.text.strip() == "(9)" for b in out)


def test_free_floating_number_is_not_attached():
    """编号不在任何栏右缘（页面中间、无对齐行）时不能挂到公式上。"""
    doc = _doc_one_page()
    blocks = [
        _equation_block("aij = qi · k⊤ j + bij", (100, 279, 200, 307)),
        _para_block("(9)", (230, 289, 250, 299)),
    ]
    out = _attach_equation_numbers(blocks, doc)
    eq = [b for b in out if b.kind == "equation"]
    assert len(eq) == 1
    assert eq[0].eq_number == ""