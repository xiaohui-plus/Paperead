"""双栏阅读顺序与文本保真回归测试。

对应缺陷：居中标题/作者行会把 gutter 投影填满，导致双栏页被判成单栏，
左右栏同一基线的文字被逐行交错拼进同一段；另外连字 ﬁ 与 Unicode
连字符断词没有还原。这里用最小版式固定这些行为。
"""

from __future__ import annotations

import pymupdf

from paperead.layout import (
    Block,
    Line,
    _join_text,
    _merge_across_pages,
    _merge_visual_rows,
    _normalize_text,
    _split_row_runs,
    parse_pdf,
)


def _pdf(path, pages):
    doc = pymupdf.open()
    for width, height, draw in pages:
        page = doc.new_page(width=width, height=height)
        draw(page)
    doc.save(str(path))
    doc.close()
    return path


def _two_column_page(page):
    page.insert_text((120, 80), "A Centered Title Spanning the Two Columns", fontsize=18)
    page.insert_text((180, 100), "Alice Author, Bob Author", fontsize=10)
    for i in range(14):
        page.insert_text(
            (60, 130 + i * 14),
            f"left column line {i:02d} carries body words",
            fontsize=10,
        )
        page.insert_text(
            (330, 130 + i * 14),
            f"right column line {i:02d} carries body words",
            fontsize=10,
        )
    page.insert_text(
        (60, 350),
        "Figure 1: A full width caption placed across the gutter and below the columns.",
        fontsize=9,
    )


def _single_column_page(page):
    for i in range(18):
        page.insert_text(
            (60, 100 + i * 16),
            f"single column paragraph line {i:02d} with a complete body sentence.",
            fontsize=10,
        )


def test_two_column_text_is_not_interleaved(tmp_path):
    pdf = _pdf(tmp_path / "twocol.pdf", [(612, 792, _two_column_page)])
    parsed = parse_pdf(pdf, paper_id="twocol", with_figures=False)

    assert parsed.columns == 2
    texts = [b.text for b in parsed.blocks if b.text.strip()]
    # 任何一块都不能同时含有左右栏同一行的文字
    for text in texts:
        for i in range(14):
            assert not (
                f"left column line {i:02d}" in text
                and f"right column line {i:02d}" in text
            ), text

    left = [b for b in parsed.blocks if "left column line 00" in b.text]
    right = [b for b in parsed.blocks if "right column line 00" in b.text]
    assert left and right
    assert left[0].order < right[0].order
    # 左栏块内不应混入右栏文字
    assert "right column" not in left[0].text
    assert "left column" not in right[0].text


def test_centered_front_matter_does_not_hide_gutter(tmp_path):
    pdf = _pdf(tmp_path / "centered.pdf", [(612, 792, _two_column_page)])
    parsed = parse_pdf(pdf, paper_id="centered", with_figures=False)
    # 居中标题与作者行都不能把双栏页变成单栏
    assert parsed.columns == 2


def test_single_column_stays_single(tmp_path):
    pdf = _pdf(tmp_path / "onecol.pdf", [(612, 792, _single_column_page)])
    parsed = parse_pdf(pdf, paper_id="onecol", with_figures=False)
    assert parsed.columns == 1


def test_ligatures_and_unicode_hyphen_are_normalized():
    assert _normalize_text("difﬁcult and ﬂow") == "difficult and flow"
    assert _normalize_text("a\u00a0b") == "a b"
    assert _join_text("inter-", "national") == "international"
    assert _join_text("multi\u2010", "target") == "multitarget"
    assert _join_text("3D\u2010", "CNN") == "3D\u2010CNN"


def _line(text, x0, x1, baseline=100.0, size=10.0, page=1, col=0):
    return Line(
        text=text,
        bbox=(x0, baseline - 8, x1, baseline + 2),
        page=page,
        size=size,
        col=col,
        baseline=baseline,
    )


def test_visual_row_does_not_merge_across_gutter():
    row = [_line("left fragment", 60, 250), _line("right fragment", 340, 540)]
    assert len(_split_row_runs(row, gutter=300.0)) == 2
    # 同一栏内正常字距仍然合并
    row = [_line("left fragment", 60, 250), _line("more text", 254, 400)]
    assert len(_split_row_runs(row, gutter=300.0)) == 1


def test_visual_row_breaks_on_large_gap_without_gutter():
    near = [_line("alpha words", 60, 200), _line("beta words", 210, 350)]
    assert len(_split_row_runs(near, gutter=None)) == 1
    far = [_line("alpha words", 60, 200), _line("beta words", 320, 460)]
    assert len(_split_row_runs(far, gutter=None)) == 2


def test_cross_page_paragraph_keeps_bbox_per_page():
    a = Block("b0", 0, 1, "para", "The paragraph continues with more details and", (60, 600, 400, 620))
    a.bboxes = [(1, 60, 600, 400, 620)]
    b = Block("b1", 1, 2, "para", "additional results that confirm the finding.", (60, 80, 400, 100))
    b.bboxes = [(2, 60, 80, 400, 100)]

    merged = _merge_across_pages([a, b])
    assert len(merged) == 1
    assert "additional results" in merged[0].text
    assert [x[0] for x in merged[0].bboxes] == [1, 2]
    assert merged[0].to_dict()["bboxes"][1][0] == 2


def test_merged_visual_row_keeps_column_after_gutter_split():
    row = [
        _line("left fragment", 60, 250, col=-1),
        _line("right fragment", 340, 540, col=-1),
    ]
    rows = _merge_visual_rows(row, {1: 300.0})
    assert len(rows) == 2
    assert sorted(r.col for r in rows) == [0, 1]