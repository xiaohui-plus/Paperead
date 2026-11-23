"""L2：跨列块拆分（左栏 + 右栏被并成一块时按栏切开）。"""

from __future__ import annotations

from paperead.layout import Block, Line, _split_cross_column_blocks


def _line(text: str, x0: float, x1: float, y0: float, col: int) -> Line:
    return Line(
        text=text,
        bbox=(x0, y0, x1, y0 + 10),
        page=1,
        size=10,
        col=col,
        baseline=y0 + 8,
    )


def _para(text: str, bbox=tuple) -> Block:
    return Block(id="b0000", order=0, page=1, kind="para", text=text, bbox=bbox)


def test_split_left_and_right_columns() -> None:
    left = [
        _line("Left column sentence one.", 50, 290, 100, 0),
        _line("Left column sentence two.", 50, 290, 114, 0),
    ]
    right = [
        _line("Right column sentence one.", 310, 550, 100, 1),
        _line("Right column sentence two.", 310, 550, 114, 1),
    ]
    text = "Left column sentence one. Left column sentence two. Right column sentence one. Right column sentence two."
    blocks = [_para(text, bbox=(50, 100, 550, 124))]
    out = _split_cross_column_blocks(blocks, left + right, {1: 300.0}, 600.0)

    assert len(out) == 2
    assert "Left" in out[0].text and "Right" not in out[0].text
    assert "Right" in out[1].text and "Left" not in out[1].text
    assert out[0].bbox[2] <= 300 < out[1].bbox[0]
    # 词守恒：两块拼回去等于原块（忽略空白与顺序差异由多重集合保证）
    joined = out[0].text + " " + out[1].text
    assert sorted(joined.split()) == sorted(text.split())
    assert [b.id for b in out] == ["b0000", "b0001"]


def test_full_width_line_blocks_split() -> None:
    lines = [
        _line("Left column text here.", 50, 290, 100, 0),
        _line("A spanning caption line in the middle.", 50, 550, 114, -1),
        _line("Right column text here.", 310, 550, 128, 1),
    ]
    block = _para("x" * 100, bbox=(50, 100, 550, 138))
    out = _split_cross_column_blocks([block], lines, {1: 300.0}, 600.0)
    assert len(out) == 1 and out[0] is block


def test_inline_math_block_not_split() -> None:
    lines = [
        _line("Left column text here.", 50, 290, 100, 0),
        _line("Right column text here.", 310, 550, 100, 1),
    ]
    block = Block(
        id="b0000",
        order=0,
        page=1,
        kind="para",
        text="Left column text here. Right column text here.",
        bbox=(50, 100, 550, 110),
        inline_math=[{"start": 0, "end": 3, "bbox": [50, 100, 70, 110]}],
    )
    out = _split_cross_column_blocks([block], lines, {1: 300.0}, 600.0)
    assert len(out) == 1 and out[0] is block


def test_no_gutter_page_not_split() -> None:
    lines = [
        _line("Left column text here.", 50, 290, 100, 0),
        _line("Right column text here.", 310, 550, 100, 1),
    ]
    block = _para("Left column text here. Right column text here.", bbox=(50, 100, 550, 110))
    out = _split_cross_column_blocks([block], lines, {1: None}, 600.0)
    assert len(out) == 1 and out[0] is block

def test_front_page_sidebar_block_split_without_gutter() -> None:
    """首页「单栏正文 + 左侧信息栏」：没有 gutter，也要按侧栏边界拆开。"""
    left = [
        _line("Department of Oncology, Boston, MA, USA", 60, 160, 100, 0),
        _line("Frontiers in Plant Science", 80, 160, 114, 0),
        _line("Received: 31 May 2021", 82, 160, 128, 0),
    ]
    right = [
        _line("Right body sentence one.", 180, 550, 100, 0),
        _line("Right body sentence two.", 180, 550, 114, 0),
    ]
    text = (
        "Department of Oncology, Boston, MA, USA Frontiers in Plant Science "
        "Received: 31 May 2021 Right body sentence one. Right body sentence two."
    )
    block = _para(text, bbox=(60, 100, 550, 138))
    out = _split_cross_column_blocks(
        [block], left + right, {1: None}, 600.0, {1: [(0.0, 160.5)]}
    )

    assert len(out) == 2
    side, body = out[0], out[1]
    assert side.column_role == "sidebar"
    # 侧栏原地翻译：机构、期刊栏目、收稿日期都要译，只有邮箱/URL/DOI/
    # ORCID 这类联系方式保留原文，所以这里仍是可译块。
    assert side.translatable is True
    assert side.bbox[2] <= 160.5
    assert "Department of Oncology" in side.text
    assert body.column_role == "body"
    assert body.translatable is True
    assert body.bbox[0] >= 179.0
    assert "Right body sentence one." in body.text
    assert sorted((side.text + " " + body.text).split()) == sorted(text.split())


def test_column_width_paragraph_not_split() -> None:
    """普通栏宽段落（<=0.5 页宽）不进入拆分。"""
    lines = [
        _line("A normal single-column paragraph line.", 60, 290, 100, 0),
        _line("Another normal paragraph line here.", 60, 290, 114, 0),
    ]
    block = _para(
        "A normal single-column paragraph line. Another normal paragraph line here.",
        bbox=(60, 100, 290, 124),
    )
    out = _split_cross_column_blocks([block], lines, {1: 300.0}, 600.0, {})
    assert len(out) == 1 and out[0] is block
