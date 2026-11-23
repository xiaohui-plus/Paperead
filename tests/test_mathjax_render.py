"""MathJax（完整 TeX 引擎）公式渲染。"""

from __future__ import annotations

import pymupdf
import pytest

from paperead.mathjax_render import available, mathjax_pdf
from paperead.mathrender import latex_pdf

pytestmark = pytest.mark.skipif(not available(), reason="node / MathJax 资源不可用")


def _open(data: bytes) -> pymupdf.Page:
    doc = pymupdf.open(stream=data, filetype="pdf")
    return doc[0]


def _is_vector(page: pymupdf.Page) -> bool:
    return len(page.get_images()) == 0 and len(page.get_drawings()) > 0


def test_renders_aligned_environment() -> None:
    data = mathjax_pdf(r"\begin{aligned} a &= b \\ c &= d \end{aligned}", 9.5)
    assert data
    page = _open(data)
    assert _is_vector(page)
    assert page.rect.height > page.rect.width * 0.5


def test_renders_cases_environment() -> None:
    data = mathjax_pdf(r"\begin{cases} 1 & x>0 \\ 0 & x\le 0 \end{cases}", 9.5)
    assert data
    assert _is_vector(_open(data))


def test_renders_matrix_environment() -> None:
    data = mathjax_pdf(r"\begin{bmatrix} 1 & 2 \\ 3 & 4 \end{bmatrix}", 9.5)
    assert data
    assert _is_vector(_open(data))


def test_renders_simple_formula_with_subscripts() -> None:
    data = mathjax_pdf(r"\mathcal{P}^{query}_{\mathrm{SPD}(i,j)}", 9.5)
    assert data
    assert _is_vector(_open(data))


def test_cache_returns_same_bytes() -> None:
    body = r"\sum_{i=1}^{n}\alpha_i x_i"
    first = mathjax_pdf(body, 9.5)
    second = mathjax_pdf(body, 9.5)
    assert first is not None and first is second


@pytest.mark.parametrize("bad", [r"\frac{", r"a={b", r"\int_{0}^{1"])
def test_rejects_unbalanced_latex(bad: str) -> None:
    assert mathjax_pdf(bad, 9.5) is None


def test_latex_pdf_prefers_full_engine() -> None:
    data = latex_pdf(r"\begin{aligned} a &= b \\ c &= d \end{aligned}", 9.5)
    assert data
    assert _is_vector(_open(data))