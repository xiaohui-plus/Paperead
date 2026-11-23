"""栏内排版内核 P1：缩进、栏宽、行距、公式不拆、图表避让。"""

from __future__ import annotations

from paperead.pageflow import PageFlow, PageSkeleton, Paragraph, Segment
from paperead.pagelayout import (
    LayoutStyle,
    assert_line_boxes,
    layout_page_flow,
    layout_paragraph,
)

STYLE = LayoutStyle(body_size=10.0, line_height=1.15)
SKELETON = PageSkeleton(
    page=1,
    width=600.0,
    height=800.0,
    columns=((50.0, 290.0), (310.0, 550.0)),
    layout="dual",
)


def _para(text, anchor=(50.0, 100.0, 290.0, 300.0), column=0, segments=None):
    return Paragraph(
        id="p1",
        page=1,
        column=column,
        kind="para",
        level=0,
        anchor=anchor,
        segments=segments or [Segment(type="text", value=text)],
    )


def test_first_line_indented_two_chars() -> None:
    lines, _ = layout_paragraph(_para("中文内容" * 20), (50.0, 290.0), STYLE, [], 760.0)
    assert lines
    assert abs(lines[0].x0 - (50.0 + STYLE.indent)) < 0.6


def test_paragraph_starts_at_anchor_top() -> None:
    lines, _ = layout_paragraph(_para("中文内容" * 20), (50.0, 290.0), STYLE, [], 760.0)
    assert abs(lines[0].y0 - 100.0) < 0.6


def test_lines_stay_inside_column() -> None:
    lines, _ = layout_paragraph(_para("中文内容" * 40), (50.0, 290.0), STYLE, [], 760.0)
    assert lines
    for line in lines:
        assert line.x0 >= 49.5
        assert line.x1 <= 290.5


def test_line_pitch_equals_size_times_height() -> None:
    lines, _ = layout_paragraph(_para("中文内容" * 40), (50.0, 290.0), STYLE, [], 760.0)
    assert len(lines) >= 2
    pitch = lines[1].baseline - lines[0].baseline
    assert abs(pitch - STYLE.pitch) < 0.6


def test_non_last_lines_are_justified() -> None:
    # 栏宽取 236：首行可用 216、正文行 236，都不是 10pt 的整数倍，
    # 断行后一定留下可分配余量，才能检验两端对齐。
    lines, _ = layout_paragraph(_para("中文内容" * 40), (50.0, 286.0), STYLE, [], 760.0)
    justified = [line for line in lines[:-1] if line.justified]
    assert justified
    for line in justified:
        assert abs(line.x1 - 286.0) < 0.6


def test_inline_math_is_atomic() -> None:
    segments = [
        Segment(type="text", value="中文前文" * 3),
        Segment(type="math", value="x=1", math_key=0, latex="x=1"),
        Segment(type="text", value="中文后文" * 30),
    ]
    lines, _ = layout_paragraph(_para("", segments=segments), (50.0, 290.0), STYLE, [], 760.0)
    counts = [sum(1 for placed in line.placed if placed.atom.type == "math") for line in lines]
    assert sum(counts) == 1
    assert max(counts) == 1


def test_figure_obstacle_is_avoided() -> None:
    paragraph = _para("中文内容" * 60, anchor=(50.0, 100.0, 290.0, 400.0))
    lines, _ = layout_paragraph(
        paragraph, (50.0, 290.0), STYLE, [(50.0, 150.0, 290.0, 200.0)], 760.0
    )
    assert lines
    for line in lines:
        assert line.y1 <= 150.5 or line.y0 >= 199.5


def test_overflow_when_column_bottom_reached() -> None:
    paragraph = _para("中文内容" * 200, anchor=(50.0, 100.0, 290.0, 760.0))
    lines, full = layout_paragraph(paragraph, (50.0, 290.0), STYLE, [], 300.0)
    assert full is True
    assert lines
    assert all(line.y1 <= 300.5 for line in lines)


def test_layout_page_flow_passes_line_box_assertions() -> None:
    flow = PageFlow(
        page=1,
        skeleton=SKELETON,
        paragraphs=[
            _para("左栏中文内容" * 20, anchor=(50.0, 100.0, 290.0, 300.0), column=0),
            _para("右栏中文内容" * 20, anchor=(310.0, 100.0, 550.0, 300.0), column=1),
        ],
    )
    lines, overflow = layout_page_flow(flow, [], STYLE)
    assert overflow == []
    assert assert_line_boxes(lines, flow) == []
    right = [line for line in lines if line.column == 1]
    assert right and all(line.x0 >= 309.5 and line.x1 <= 550.5 for line in right)


def test_assert_line_boxes_flags_column_leak() -> None:
    from paperead.pagelayout import LineBox, PlacedAtom, Atom

    line = LineBox(
        page=1,
        column=0,
        paragraph_id="p1",
        baseline=110.0,
        y0=100.0,
        y1=111.5,
        x0=50.0,
        x1=420.0,
        justified=False,
        placed=[PlacedAtom(atom=Atom("text", "x", 370.0), x=50.0, width=370.0)],
    )
    flow = PageFlow(page=1, skeleton=SKELETON, paragraphs=[])
    problems = assert_line_boxes([line], flow)
    assert any("越出栏界" in item for item in problems)