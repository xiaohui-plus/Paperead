"""页内段落流 P0：版式骨架、栏归属、段落合并、跨页切片守恒。"""

from __future__ import annotations

from paperead.pageflow import (
    COLUMN_SIDEBAR,
    COLUMN_SPAN,
    assert_cross_page_conservation,
    build_page_flow,
    split_text_by_pages,
)

W, H = 595.0, 842.0


def _b(block_id, page, kind, text, bbox, size=9.5, role=""):
    return {
        "id": block_id,
        "page": page,
        "kind": kind,
        "text": text,
        "bbox": bbox,
        "size": size,
        "column_role": role,
        "translatable": True,
    }


def _dual_page() -> list[dict]:
    return [
        _b("l1", 1, "para", "Left paragraph one is long enough here.", (50, 100, 290, 140)),
        _b("l2", 1, "para", "Left paragraph two is also long enough.", (50, 160, 290, 200)),
        _b("l3", 1, "para", "Left paragraph three wraps up the column.", (50, 220, 290, 260)),
        _b("r1", 1, "para", "Right paragraph one is long enough here.", (305, 100, 550, 140)),
        _b("r2", 1, "para", "Right paragraph two is also long enough.", (305, 160, 550, 200)),
        _b("r3", 1, "para", "Right paragraph three wraps up the column.", (305, 220, 550, 260)),
    ]


def test_infer_dual_column_skeleton() -> None:
    flows = build_page_flow(_dual_page(), {1: (W, H)})
    skeleton = flows[1].skeleton
    assert skeleton.layout == "dual"
    assert skeleton.is_dual
    assert skeleton.gutter is not None
    left_end, right_start = skeleton.gutter
    assert left_end <= right_start


def test_dual_with_sidebar() -> None:
    blocks = _dual_page() + [
        _b("s1", 1, "para", "Edited by: someone, University of X, USA",
           (15, 100, 145, 140), role="sidebar"),
        _b("s2", 1, "para", "Reviewed by: another person, Institute Y",
           (15, 150, 145, 190), role="sidebar"),
    ]
    flows = build_page_flow(blocks, {1: (W, H)})
    skeleton = flows[1].skeleton
    assert skeleton.layout == "dual_sidebar"
    assert skeleton.sidebar is not None
    side_paragraphs = [p for p in flows[1].paragraphs if p.column == COLUMN_SIDEBAR]
    assert len(side_paragraphs) == 2


def test_span_block_gets_full_width_column() -> None:
    blocks = _dual_page() + [
        _b("t1", 1, "title", "A full width paper title goes across both columns",
           (50, 40, 550, 70), size=16),
    ]
    flows = build_page_flow(blocks, {1: (W, H)})
    title = next(p for p in flows[1].paragraphs if p.id == "t1")
    assert title.column == COLUMN_SPAN


def test_left_and_right_assignment() -> None:
    flows = build_page_flow(_dual_page(), {1: (W, H)})
    by_id = {p.id: p for p in flows[1].paragraphs}
    assert by_id["l1"].column == 0
    assert by_id["r1"].column == 1


def test_broken_sentence_merges_into_one_paragraph() -> None:
    blocks = [
        _b("b1", 1, "para", "We observed that E-GP outperforms", (50, 100, 290, 112)),
        _b("b2", 1, "para", "benchmark GP in all scenarios and settings.", (50, 116, 290, 128)),
        _b("b3", 1, "para", "Another independent paragraph starts here.", (50, 140, 290, 152)),
    ]
    flows = build_page_flow(blocks, {1: (W, H)})
    paragraphs = flows[1].paragraphs
    assert len(paragraphs) == 2
    assert paragraphs[0].text.endswith("benchmark GP in all scenarios and settings.")
    assert paragraphs[0].anchor == (50, 100, 290, 128)


def test_finished_sentence_does_not_merge() -> None:
    blocks = [
        _b("b1", 1, "para", "This paragraph is already complete.", (50, 100, 290, 112)),
        _b("b2", 1, "para", "A new paragraph starts here.", (50, 116, 290, 128)),
    ]
    flows = build_page_flow(blocks, {1: (W, H)})
    assert len(flows[1].paragraphs) == 2


def test_cross_column_fragments_do_not_merge() -> None:
    blocks = _dual_page() + [
        _b("x", 1, "para", "Unfinished sentence on the left", (50, 280, 290, 292)),
        _b("y", 1, "para", "continuation on the right column", (305, 280, 550, 292)),
    ]
    flows = build_page_flow(blocks, {1: (W, H)})
    left = next(p for p in flows[1].paragraphs if p.id == "x")
    right = next(p for p in flows[1].paragraphs if p.id == "y")
    assert left.column == 0 and right.column == 1
    assert "continuation" not in left.text


def test_split_text_by_pages_conserves_text() -> None:
    text = (
        "First sentence of the paragraph. Second sentence continues here. "
        "Third sentence closes the paragraph."
    )
    pieces = split_text_by_pages(text, [60.0, 40.0])
    assert len(pieces) == 2
    assert "".join(pieces) == text
    assert pieces[0][-1] in ".!?。！？ " or pieces[0].endswith(" ")


def test_cross_page_block_conservation_assert() -> None:
    blocks = [
        _b("c1", 1, "para",
           "First sentence of a long paragraph. Second sentence continues. "
           "Third sentence closes it.",
           (50, 700, 550, 760)),
    ]
    blocks[0]["bboxes"] = [
        (1, 50, 700, 550, 760),
        (2, 50, 60, 550, 120),
    ]
    flows = build_page_flow(blocks, {1: (W, H), 2: (W, H)})
    assert assert_cross_page_conservation(blocks, flows) == []
    page1 = [p for p in flows[1].paragraphs if p.cross_page_group == "c1"]
    page2 = [p for p in flows[2].paragraphs if p.cross_page_group == "c1"]
    assert page1 and page2
    assert "".join(p.text for p in page1 + page2) == blocks[0]["text"]