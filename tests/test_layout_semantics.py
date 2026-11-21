"""语义结构识别回归：宽字距摘要标题、跨页前置区与文末作者信息。"""

from __future__ import annotations

from paperead.layout import (
    Block,
    Line,
    _classify,
    _detect_front_matter,
    _is_abstract_head,
    _split_row_runs,
)


def _line(
    text: str,
    x0: float,
    y0: float,
    x1: float,
    *,
    size: float = 10.0,
    bold: bool = False,
    col: int = 0,
    page: int = 1,
) -> Line:
    return Line(
        text=text,
        bbox=(x0, y0, x1, y0 + size * 1.1),
        page=page,
        size=size,
        bold=bold,
        col=col,
        baseline=y0 + size,
    )


def _block(order: int, kind: str, text: str, page: int, y: float, size: float = 10.0) -> Block:
    return Block(
        id=f"b{order:04d}",
        order=order,
        page=page,
        kind=kind,
        text=text,
        bbox=(40.0, y, 560.0, y + size * 1.4),
        size=size,
    )


def test_spaced_abstract_heading_forms_are_recognized():
    for text in ("ABSTRACT", "A B S T R A C T", "a b s t r a c t", "Abstract", "摘 要"):
        assert _is_abstract_head(text), text
    assert not _is_abstract_head("Abstraction")


def test_side_by_side_labels_are_split_by_wide_gap():
    row = [
        _line("a r t i c l e i n f o", 37.9, 250.9, 136.4, size=9.0),
        _line("a b s t r a c t", 202.3, 253.2, 271.0, size=9.0),
    ]
    runs = _split_row_runs(row, gutter=297.9)
    assert len(runs) == 2
    assert runs[0][0].text.startswith("a r t i c l e")
    assert runs[1][0].text.startswith("a b s t r a c t")


def test_abstract_heading_on_second_page_is_detected():
    lines = [
        _line("Highlights", 60, 40, 200, size=20),
        _line("We propose a new graph model for recommendation.", 60, 120, 550),
        _line("Experiments cover four public benchmark datasets.", 60, 140, 550),
        _line("A dual-path framework for preference evolution", 60, 40, 550, size=20, page=2),
        _line("A B S T R A C T", 200, 120, 271, size=9, page=2),
        _line("This paper studies graph learning for users and items.", 200, 140, 560, size=9, page=2),
        _line("It improves ranking accuracy on every benchmark.", 200, 152, 560, size=9, page=2),
        _line("Keywords: graph, recommendation", 40, 140, 160, size=7, page=2),
        _line("1. Introduction", 60, 220, 200, page=2),
    ]
    front = _detect_front_matter(lines, body_size=10.0, page_height=792, page_width=612)
    assert front.page == 2
    assert front.abstract is not None
    assert front.in_abstract(lines[5])
    assert front.in_abstract(lines[6])


def test_tail_metadata_is_folded_and_appendix_returns_to_body():
    blocks = [
        _block(0, "title", "A Dual Path Framework for Preference Evolution", 1, 50, size=20),
        _block(1, "para", "Body text before the reference list in the article.", 1, 120),
        _block(2, "heading", "References", 2, 60),
        _block(3, "reference", "[1] Smith et al. 2020. A reference entry title.", 2, 80),
        _block(
            4,
            "para",
            "Author contributions J.G. and A.K. designed the study and wrote the manuscript.",
            2,
            100,
        ),
        _block(5, "para", "Funding This work was supported by NIH grant 1R01DC021133.", 2, 120),
        _block(6, "para", "Competing interests The authors declare no competing interests.", 2, 140),
        _block(7, "heading", "APPENDIX", 2, 160),
        _block(8, "para", "Appendix body text that must stay in the reading flow.", 2, 180),
    ]
    _classify(blocks, body_size=10.0, page_height=792.0)
    assert blocks[3].kind == "reference"
    assert blocks[4].kind == "footnote"
    assert blocks[5].kind == "footnote"
    assert blocks[6].kind == "footnote"
    assert blocks[7].kind == "heading"
    assert blocks[8].kind == "para"


def test_midpage_funding_paragraph_stays_in_body():
    blocks = [
        _block(0, "title", "A Dual Path Framework for Preference Evolution", 1, 50, size=20),
        _block(
            1,
            "para",
            "Funding agencies increasingly require detailed reporting of supported projects.",
            3,
            300,
        ),
    ]
    _classify(blocks, body_size=10.0, page_height=792.0)
    assert blocks[1].kind == "para"