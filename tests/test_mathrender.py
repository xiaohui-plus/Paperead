"""LaTeX → 矢量 PDF 片段（mathtext）。"""

from __future__ import annotations

import pymupdf

from paperead.mathrender import (
    HAS_MATHTEXT,
    insert_latex,
    latex_pdf,
    source_to_latex,
)


def test_source_to_latex_maps_symbols() -> None:
    assert "\\alpha" in source_to_latex("α + β")
    assert "\\leq" in source_to_latex("a ≤ b")
    assert "\\times" in source_to_latex("2 × 3")


def test_latex_pdf_renders_common_formula() -> None:
    if not HAS_MATHTEXT:
        return
    data = latex_pdf(r"\alpha+\beta=\frac{a}{b}", 10.0)
    assert data
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        rect = doc[0].rect
        assert rect.width > 5 and rect.height > 5
    finally:
        doc.close()


def test_latex_pdf_rejects_invalid_formula() -> None:
    assert latex_pdf(r"\frac{", 10.0) is None


def test_insert_latex_is_vector() -> None:
    if not HAS_MATHTEXT:
        return
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=120)
    ok = insert_latex(page, pymupdf.Rect(20, 20, 160, 70), r"y=1\mu+Z_Au_A", 10.0)
    assert ok
    # 矢量：嵌入后页面不应出现图像 XObject
    assert page.get_images() == []
    doc.close()