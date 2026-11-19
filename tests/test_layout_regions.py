"""页面区域分类回归测试。

对应缺陷：行号被并进正文、图内标注被当作正文段落、公式矢量块被裁成图片。
这三类都属于「建块前没有区域概念」，因此固定在 regions 层验证。
"""

from __future__ import annotations

import pymupdf

from paperead.regions import (
    Region,
    build_page_regions,
    detect_float_regions,
    detect_line_numbers,
)


class _Line:
    def __init__(self, text, bbox, page=1, col=0, size=10.0, math=False, margin=False):
        self.text = text
        self.bbox = bbox
        self.page = page
        self.col = col
        self.size = size
        self.math = math
        self.is_margin = margin


def _body_page(page):
    for i in range(18):
        page.insert_text(
            (80, 120 + i * 14),
            f"body line {i:02d} carries a complete sentence of the paper.",
            fontsize=10,
        )


def _flowchart_page(page):
    for i in range(6):
        rect = pymupdf.Rect(90, 90 + i * 72, 300, 145 + i * 72)
        page.draw_rect(rect, color=(0, 0, 0), width=1.5)
        page.insert_text((110, 120 + i * 72), f"Step {i} pool", fontsize=8)
    page.draw_line(pymupdf.Point(195, 145), pymupdf.Point(195, 162), width=1.2)
    for i in range(10):
        page.insert_text(
            (80, 560 + i * 14),
            f"Figure discussion line {i:02d} belongs to the body text.",
            fontsize=10,
        )


def _pdf(path, draw):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    draw(page)
    doc.save(str(path))
    doc.close()
    return path


def test_line_numbers_are_detected_and_body_lines_are_not():
    lines: list[_Line] = []
    for i in range(20):
        lines.append(_Line(
            f"body line {i:02d} carries a sentence",
            (80.0, 100.0 + i * 14, 520.0, 110.0 + i * 14),
        ))
    for n, i in enumerate((0, 5, 10, 15)):
        lines.append(_Line(
            str((n + 1) * 5),
            (58.0, 100.0 + i * 14, 66.0, 109.0 + i * 14),
            size=8.0,
        ))

    found = detect_line_numbers(lines, body_size=10.0)
    assert len(found) == 4
    assert all(r.kind == "line_number" for r in found)


def test_numbers_inside_column_are_not_line_numbers():
    lines: list[_Line] = []
    for i in range(12):
        lines.append(_Line(
            f"body line {i:02d} carries a sentence",
            (80.0, 100.0 + i * 14, 520.0, 110.0 + i * 14),
        ))
    for i in (0, 4, 8):
        lines.append(_Line(
            "7",
            (200.0, 100.0 + i * 14, 208.0, 109.0 + i * 14),
            size=8.0,
        ))
    assert detect_line_numbers(lines, body_size=10.0) == []


def test_irregular_number_spacing_is_not_a_line_number_column():
    lines: list[_Line] = []
    for i in range(16):
        lines.append(_Line(
            f"body line {i:02d} carries a sentence",
            (80.0, 100.0 + i * 14, 520.0, 110.0 + i * 14),
        ))
    for i in (0, 1, 9):
        lines.append(_Line(
            str(i + 1),
            (58.0, 100.0 + i * 14, 66.0, 109.0 + i * 14),
            size=8.0,
        ))
    assert detect_line_numbers(lines, body_size=10.0) == []


def test_vector_figure_with_labels_is_detected_without_captions(tmp_path):
    pdf = _pdf(tmp_path / "flow.pdf", _flowchart_page)
    doc = pymupdf.open(str(pdf))
    page = doc[0]
    lines = [
        _Line(f"Step {i} pool", (110.0, 108.0 + i * 72, 190.0, 122.0 + i * 72), size=8.0)
        for i in range(6)
    ]
    regions = detect_float_regions(page, 1, lines, body_size=10.0)
    doc.close()

    assert regions, "带标注的流程图必须被识别成图区"
    assert any(r.bbox[1] < 120 and r.bbox[3] > 480 for r in regions), regions


def test_body_lines_inside_cluster_veto_the_figure(tmp_path):
    """簇内一旦出现成段正文，整块区域必须否决。

    宁可漏一张图，也不能把正文裁进图片——这也是区域层放在建块之前
    的前提：如果反过来先信图区，正文会整段消失且不可恢复。
    """
    pdf = _pdf(tmp_path / "body.pdf", _flowchart_page)
    doc = pymupdf.open(str(pdf))
    page = doc[0]
    lines = [
        _Line(f"Step {i} pool", (110.0, 108.0 + i * 72, 190.0, 122.0 + i * 72), size=8.0)
        for i in range(6)
    ]
    lines.append(_Line(
        "this is a full body paragraph " * 9,
        (100.0, 300.0, 460.0, 314.0),
        size=10.0,
    ))
    regions = detect_float_regions(page, 1, lines, body_size=10.0)
    doc.close()
    assert regions == []


def test_math_lines_veto_a_vector_cluster(tmp_path):
    pdf = _pdf(tmp_path / "math.pdf", _flowchart_page)
    doc = pymupdf.open(str(pdf))
    page = doc[0]
    lines = [
        _Line("x = y + z", (110.0, 108.0 + i * 72, 190.0, 122.0 + i * 72), math=True)
        for i in range(6)
    ]
    regions = detect_float_regions(page, 1, lines, body_size=10.0)
    doc.close()
    assert regions == []


def test_page_regions_exposes_tables_and_line_numbers(tmp_path):
    pdf = _pdf(tmp_path / "mixed.pdf", _body_page)
    doc = pymupdf.open(str(pdf))
    lines = [
        _Line(f"body line {i}", (80.0, 120.0 + i * 14, 520.0, 132.0 + i * 14))
        for i in range(18)
    ]
    table = pymupdf.Rect(80, 120, 520, 200)
    result = build_page_regions(
        doc, lines, body_size=10.0, table_rects={1: [(table, "", 3)]},
    )
    doc.close()
    assert 1 in result
    assert result[1].tables and result[1].tables[0].kind == "table"