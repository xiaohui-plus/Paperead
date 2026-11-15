"""图注锚定的矢量图与无框/整页表格回归测试。

对应问题：带文字标注的矢量图（热力图、流程图）被「区域内无文字」判据排除；
整页大表被「吞页」判据误杀；无框表在 lines 策略命中后不再跑 text 策略。
"""

from __future__ import annotations

import pymupdf
import pytest

from paperead.layout import parse_pdf


def _text_pdf(path, pages):
    doc = pymupdf.open()
    for draw in pages:
        page = doc.new_page(width=612, height=792)
        draw(page)
    doc.save(str(path))
    doc.close()
    return path


def test_vector_heatmap_with_labels_becomes_figure(tmp_path):
    def draw(page):
        body = (
            "Recent work has studied validator accuracy across scientific domains. "
            "We compare expert and non-expert validators on questions drawn from "
            "biology, chemistry and physics, and report the resulting matrix below. "
        )
        y = 60
        for _ in range(4):
            page.insert_text((50, y), body, fontsize=10)
            y += 14

        x0, y0, cw, ch = 150, 240, 70, 55
        colors = [
            (0.95, 0.90, 0.70), (0.63, 0.85, 0.71), (0.25, 0.71, 0.77),
            (0.17, 0.50, 0.72), (0.15, 0.20, 0.58),
        ]
        for r in range(3):
            for c in range(3):
                rect = pymupdf.Rect(x0 + c * cw, y0 + r * ch, x0 + (c + 1) * cw, y0 + (r + 1) * ch)
                page.draw_rect(rect, color=None, fill=colors[min(4, r + c)])
                page.insert_text((rect.x0 + 10, rect.y0 + 22), f"{40 + r * 10 + c}.0", fontsize=8)
                page.insert_text((rect.x0 + 12, rect.y0 + 36), f"n={100 + c}", fontsize=6)
        page.draw_rect(
            pymupdf.Rect(x0, y0, x0 + 3 * cw, y0 + 3 * ch), color=(0, 0, 0), width=0.8
        )
        for r, name in enumerate(["Biology", "Chemistry", "Physics"]):
            page.insert_text((70, y0 + r * ch + 30), name, fontsize=8)
        for c, name in enumerate(["Biologists", "Chemists", "Physicists"]):
            page.insert_text((x0 + c * cw + 6, y0 + 3 * ch + 16), name, fontsize=7)
        page.insert_text((60, 520), "Figure 2: Heatmap of validator accuracy by domain and expertise.",
                         fontsize=9)

    pdf = _text_pdf(tmp_path / "heatmap.pdf", [draw])
    parsed = parse_pdf(pdf, paper_id="heat", figure_dir=tmp_path / "figs")
    figs = [b for b in parsed.blocks if b.kind == "figure"]
    assert len(figs) == 1
    assert figs[0].caption.startswith("Figure 2")
    assert figs[0].image_path and (tmp_path / "figs").exists()
    # 图内数字不应该再作为公式碎块留在阅读流里
    assert not any(
        b.kind == "equation" and "n=" in b.text for b in parsed.blocks
    )
    # 裁图要包含标题（阅读页 caption 同时保留，允许重复显示）
    cap = [
        b for b in parsed.blocks
        if b.kind == "caption" and b.text.startswith("Figure 2")
    ][0]
    assert figs[0].bbox[0] <= cap.bbox[0] + 2
    assert figs[0].bbox[1] <= cap.bbox[1] + 2
    assert figs[0].bbox[2] >= cap.bbox[2] - 2
    assert figs[0].bbox[3] >= cap.bbox[3] - 2


def test_vector_flowchart_becomes_figure(tmp_path):
    def draw(page):
        body = (
            "Our pipeline consists of writing, validation and revision stages, and "
            "each stage is carried out by a different group of participants. The "
            "overall goal is to keep only questions that experts agree on. "
        )
        y = 40
        for _ in range(5):
            page.insert_text((50, y), body, fontsize=10)
            y += 14

        def box(rect, fill, lines):
            page.draw_rect(rect, color=None, fill=fill)
            yy = rect.y0 + 16
            for ln in lines:
                page.insert_text((rect.x0 + 8, yy), ln, fontsize=7)
                yy += 10

        box(pymupdf.Rect(70, 90, 250, 170), (0.94, 0.91, 0.98),
            ["Question writing", "Question, options", "Correct answer (b)"])
        box(pymupdf.Rect(290, 90, 470, 170), (0.90, 0.94, 0.99),
            ["Expert validation #1", "Answer Q", "Post-hoc agreement"])
        box(pymupdf.Rect(290, 210, 470, 300), (0.94, 0.91, 0.98),
            ["Question revision", "Revised question", "Explanation"])
        box(pymupdf.Rect(70, 350, 470, 440), (0.92, 0.97, 0.93),
            ["Non-expert validation", "At least 15 minutes, Google allowed",
             "Non-expert #1 (c) X"])
        box(pymupdf.Rect(60, 520, 480, 580), (1, 1, 1),
            ["Include this Q in the DIAMOND set because",
             "2 out of 2 expert validators agree"])
        page.draw_line((250, 130), (290, 130), width=1.0)
        page.draw_line((470, 165), (500, 165), width=1.0)
        page.draw_line((360, 300), (360, 350), width=1.0)
        page.insert_text(
            (50, 700),
            "Figure 1: Our data creation pipeline, including question writing, "
            "expert validation, revision and non-expert validation.",
            fontsize=9,
        )

    pdf = _text_pdf(tmp_path / "flow.pdf", [draw])
    parsed = parse_pdf(pdf, paper_id="flow", figure_dir=tmp_path / "figs")
    figs = [b for b in parsed.blocks if b.kind == "figure"]
    assert len(figs) == 1
    assert figs[0].caption.startswith("Figure 1")
    # 图内标注已并入裁图，不应再逐条留在正文里
    assert not any("Post-hoc agreement" == b.text.strip() for b in parsed.blocks)

    # 页面顶部的正文不能一起被吞进图区
    assert any(b.kind == "para" and "pipeline consists" in b.text for b in parsed.blocks)


def test_qa_style_full_page_table_is_kept(tmp_path):
    def draw(page):
        page.insert_text((50, 60), "Abstract", fontsize=12)
        page.insert_text(
            (50, 80),
            "We introduce a benchmark and validate expert agreement across domains; "
            "the table below lists example questions with their correct options and "
            "the corresponding explanations used during validation.",
            fontsize=10,
        )
        page.insert_text((150, 140), "Table 1: Six example questions from the dataset.",
                         fontsize=9)
        for i, name in enumerate(
            ["Chemistry (general)", "Organic Chemistry", "Genetics",
             "Molecular Biology", "Astrophysics", "Quantum Mechanics",
             "Statistical Physics", "Biochemistry"]
        ):
            page.insert_text((200, 170 + i * 60), name, fontsize=9)
            page.insert_text(
                (60, 188 + i * 60),
                "A long stem that asks about the mechanism and offers four options "
                "(A) option one (B) option two (C) option three (D) option four here.",
                fontsize=8,
            )

    pdf = _text_pdf(tmp_path / "qatable.pdf", [draw])
    parsed = parse_pdf(pdf, paper_id="qa", figure_dir=tmp_path / "figs")
    tabs = [b for b in parsed.blocks if b.kind == "table"]
    assert len(tabs) == 1
    assert tabs[0].caption.startswith("Table 1")
    assert tabs[0].image_path


def test_frameless_table_uses_text_strategy(tmp_path):
    def draw(page):
        page.insert_text((50, 70), "Table 2: Accuracy by domain and validator type.",
                         fontsize=9)
        xs = [60, 190, 300, 410, 520]
        rows = [
            ("Domain", "Count", "Expert", "NonExpert", "Delta"),
            ("Biology", "105", "66.7", "43.2", "23.5"),
            ("Physics", "227", "57.3", "32.5", "24.8"),
            ("Chemistry", "214", "72.0", "31.4", "40.6"),
        ]
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                page.insert_text((xs[ci], 110 + ri * 18), cell, fontsize=9)
        page.insert_text((50, 220), "The table lists accuracies for each scientific domain.",
                         fontsize=10)

    pdf = _text_pdf(tmp_path / "frameless.pdf", [draw])
    parsed = parse_pdf(pdf, paper_id="plain", figure_dir=tmp_path / "figs")
    tabs = [b for b in parsed.blocks if b.kind == "table"]
    assert len(tabs) == 1
    assert tabs[0].caption.startswith("Table 2")
    assert tabs[0].table_html.strip()


def test_plain_text_pages_do_not_become_figures(tmp_path):
    def draw(page):
        body = (
            "This paper studies a narrow question in computational biology and reports "
            "several controlled experiments. The method is described in detail and the "
            "results are discussed with respect to prior work in the field. "
        )
        y = 60
        for _ in range(12):
            page.insert_text((50, y), body, fontsize=10)
            y += 14

    pdf = _text_pdf(tmp_path / "plain.pdf", [draw, draw, draw])
    parsed = parse_pdf(pdf, paper_id="plain", figure_dir=tmp_path / "figs")
    assert [b for b in parsed.blocks if b.kind == "figure"] == []
    text = " ".join(b.text for b in parsed.blocks if b.kind in ("para", "heading"))
    assert text.count("computational biology") >= 36

def test_caption_after_full_width_table_row_stays_separate(tmp_path):
    """表格末行写满时，紧接的表注不能被并进同一段，否则表失去锚点。"""

    def draw(page):
        body = (
            "Recent work compares graph methods on heterogeneous datasets and reports "
            "accuracy under a unified protocol. The table below lists the numbers. "
        )
        y = 50
        for _ in range(5):
            page.insert_text((50, y), body, fontsize=10)
            y += 13
        x = 320
        page.insert_text((x, 200), "Method Year Cornell Wisconsin Actor Total", fontsize=9)
        page.insert_text((x, 212), "GCN 2017 45.67 52.55 28.73 42.31", fontsize=9)
        page.insert_text((x, 224), "GAT 2017 47.02 57.45 28.33 44.26", fontsize=9)
        page.insert_text(
            (x, 236),
            "Tokenphormer ours 76.22 86.17 37.01 66.46 62.31 55.17",
            fontsize=9,
        )
        page.insert_text(
            (x, 248),
            "Table 2: Experiments on Heterogeneous Datasets. Best results are in bold.",
            fontsize=9,
        )

    pdf = _text_pdf(tmp_path / "same_col.pdf", [draw])
    parsed = parse_pdf(pdf, paper_id="samecol", figure_dir=tmp_path / "figs")
    tabs = [b for b in parsed.blocks if b.kind == "table"]
    assert len(tabs) == 1
    assert tabs[0].caption.startswith("Table 2")


def test_repeated_figure_numbers_are_not_page_headers(tmp_path):
    """每页都是「Fig. N.」的短图注不能被跨页重复判定当成页眉删掉。"""

    def draw(page):
        page.insert_text(
            (50, 60),
            "Body text describing the experiment, the setup and the measured outcome.",
            fontsize=10,
        )
        for i in range(3):
            page.draw_rect(
                pymupdf.Rect(80 + i * 100, 100, 180 + i * 100, 200),
                color=None, fill=(0.8, 0.85, 0.95),
            )
        page.insert_text((50, 300), f"Fig. {page.number + 1}.", fontsize=9)

    pdf = _text_pdf(tmp_path / "repeat.pdf", [draw, draw, draw])
    parsed = parse_pdf(pdf, paper_id="repeat", figure_dir=tmp_path / "figs")
    figs = [b for b in parsed.blocks if b.kind == "figure"]
    assert len(figs) == 3
    assert all(f.caption.strip() for f in figs)


def test_long_vector_label_does_not_block_figure(tmp_path):
    """图内说明性长标注（字号略小于正文）不能把整张矢量图判成正文。"""

    def draw(page):
        body = (
            "RingFormer improves property prediction by using ring-level message passing "
            "over molecular graphs, and the ablation below isolates each component. "
        )
        y = 50
        for _ in range(5):
            page.insert_text((50, y), body, fontsize=10)
            y += 13
        page.draw_rect(pymupdf.Rect(60, 120, 540, 280), color=(0, 0, 0), width=1.0)
        for i in range(3):
            page.draw_line((120 + i * 120, 130), (120 + i * 120, 270), width=0.8)
        page.insert_text(
            (70, 145),
            "(a) Relative improvement of functional group nodes with ring-level "
            "cross-attention on molecules across four datasets and three random seeds.",
            fontsize=8.5,
        )
        page.insert_text((70, 165), "(b) GPS UMAP", fontsize=8.5)
        page.insert_text((70, 185), "(c) RingFormer UMAP", fontsize=8.5)
        page.insert_text(
            (50, 330),
            "Figure 3: (a) Performance improvement on molecules with varying ring complexity.",
            fontsize=9,
        )

    pdf = _text_pdf(tmp_path / "longlabel.pdf", [draw])
    parsed = parse_pdf(pdf, paper_id="longlabel", figure_dir=tmp_path / "figs")
    figs = [b for b in parsed.blocks if b.kind == "figure"]
    assert len(figs) == 1
    assert figs[0].caption.startswith("Figure 3")

def test_figure_without_caption_gets_fallback_title(tmp_path):
    """PDF 本身没有图注时，也不能在阅读页留下只有图、标题全空的块。"""

    def draw(page):
        body = (
            "This paper reports a controlled experiment and discusses the measured "
            "outcomes in detail. The figure below shows the overall pipeline. "
        )
        y = 60
        for _ in range(5):
            page.insert_text((50, y), body, fontsize=10)
            y += 14
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200), False)
        pix.clear_with(180)
        page.insert_image(pymupdf.Rect(100, 320, 500, 620), pixmap=pix)

    pdf = _text_pdf(tmp_path / "nocap.pdf", [draw])
    parsed = parse_pdf(pdf, paper_id="nocap", figure_dir=tmp_path / "figs")
    figs = [b for b in parsed.blocks if b.kind == "figure"]
    assert len(figs) == 1
    assert figs[0].caption == "第 1 页图"

def test_table_detection_shrinks_off_adjacent_figure(tmp_path, monkeypatch):
    """find_tables 把图注/子图标题也当成表格行时，表格不能把整张图吞掉。

    对应问题：NLGT 论文第 6 页的 Table 2 识别框一直延伸到 Figure 2 的子图
    标题，图区被判成「与表格重叠」直接跳过，Figure 2 既没进图表栏，又被裁
    进表格图里。
    """
    import paperead.layout as layout

    def draw(page):
        for y in [54, 81, 96, 110, 124, 138, 152, 166, 181, 195, 210]:
            page.draw_line((56, y), (556, y), color=(0, 0, 0), width=0.8)
        for i, name in enumerate(["GCN", "GraphSAGE", "GAT", "GraphGPS", "HEAL", "NLGT"]):
            page.insert_text((62, 74 + i * 14.2), name, fontsize=7)
            page.insert_text((150 + i * 40, 74 + i * 14.2), "82.1", fontsize=7)
        page.insert_text((54, 220), "Table 2: Accuracy(%) comparison on benchmark datasets.",
                         fontsize=9)

        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 50), False)
        pix.clear_with(120)
        for x in [55, 181, 307, 433]:
            page.insert_image(pymupdf.Rect(x, 254, x + 123, 361), pixmap=pix)
        page.insert_text((74, 372), "(a) Adjacency Matrix of Sparse Sub-graph", fontsize=8)
        page.insert_text((203, 372), "(b) Adjacency Matrix-based Attention", fontsize=8)
        page.insert_text((325, 372), "(c) Fully-connected Attention Without Mask", fontsize=8)
        page.insert_text((436, 372), "(d) Neighborhood-based Masked Attention in NLGT", fontsize=8)
        page.insert_text(
            (152, 402),
            "Figure 2: Visualization of Different Attention Mechanisms for Sparse Graphs",
            fontsize=9,
        )
        page.insert_text(
            (54, 440),
            "Attention Mechanisms. We further investigate whether the neighborhood-based "
            "attention allows the model to focus selectively on different nodes.",
            fontsize=10,
        )

    pdf = _text_pdf(tmp_path / "swallowed.pdf", [draw])

    def fake_tables(doc):
        # 模拟 find_tables：表框一路吞到 Figure 2 的子图标题
        return {1: [(pymupdf.Rect(56, 54, 556, 386), (
            "<table><tr><td>GCN</td><td>82.1</td></tr>"
            "<tr><td>GAT</td><td>83.0</td></tr>"
            "<tr><td>NLGT</td><td>84.0</td></tr></table>"
        ), 4)]}

    monkeypatch.setattr(layout, "_find_page_tables", fake_tables)
    parsed = parse_pdf(pdf, paper_id="swallowed", figure_dir=tmp_path / "figs")

    figs = [b for b in parsed.blocks if b.kind == "figure"]
    assert len(figs) == 1
    assert figs[0].caption.startswith("Figure 2")

    tabs = [b for b in parsed.blocks if b.kind == "table"]
    assert len(tabs) == 1
    # 表框要收在子图上沿，图区不能再被表格裁图吃掉
    assert tabs[0].bbox[3] < 260


def test_table_without_adjacent_figure_is_not_shrunk(tmp_path, monkeypatch):
    """没有图注/图区的页面，表框保持原样，避免误伤表格内的示意图。"""
    import paperead.layout as layout

    def draw(page):
        for y in [80, 110, 140, 170]:
            page.draw_line((56, y), (556, y), color=(0, 0, 0), width=0.8)
        page.insert_text((62, 100), "GCN", fontsize=9)
        page.insert_text((150, 100), "82.1", fontsize=9)
        page.insert_text((54, 190), "Table 1: Results.", fontsize=9)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 50), False)
        pix.clear_with(200)
        page.insert_image(pymupdf.Rect(300, 300, 500, 460), pixmap=pix)

    pdf = _text_pdf(tmp_path / "notcap.pdf", [draw])
    original = pymupdf.Rect(56, 80, 556, 170)

    def fake_tables(doc):
        return {1: [(pymupdf.Rect(original), (
            "<table><tr><td>GCN</td><td>82.1</td></tr>"
            "<tr><td>GAT</td><td>83.0</td></tr>"
            "<tr><td>NLGT</td><td>84.0</td></tr></table>"
        ), 4)]}

    monkeypatch.setattr(layout, "_find_page_tables", fake_tables)
    parsed = parse_pdf(pdf, paper_id="notcap", figure_dir=tmp_path / "figs")
    tabs = [b for b in parsed.blocks if b.kind == "table"]
    assert len(tabs) == 1
    assert tabs[0].bbox[3] <= 170 + 30
    figs = [b for b in parsed.blocks if b.kind == "figure"]
    assert len(figs) == 1


def test_swallow_check_ignores_narrow_long_table_cells():
    """表内长单元格只有一列宽，不能按「长段落」把整页大表判成正文。"""
    from paperead.layout import Block, _table_swallows_page

    rect = pymupdf.Rect(60, 50, 540, 700)
    blocks = [
        Block(
            id=f"s{i}", order=0, page=1, kind="para",
            text="short cell entry", bbox=(70, 60 + i * 12, 300, 70 + i * 12),
        )
        for i in range(20)
    ]
    blocks += [
        Block(id="a", order=0, page=1, kind="para",
              text="reference list " * 25, bbox=(70, 400, 260, 420)),
        Block(id="b", order=0, page=1, kind="para",
              text="benchmark list " * 25, bbox=(280, 400, 480, 420)),
        Block(id="outside", order=0, page=1, kind="para",
              text="body paragraph " * 40, bbox=(60, 720, 540, 740)),
    ]
    assert _table_swallows_page(1, rect, blocks) is False


def test_swallow_check_still_rejects_wide_body_paragraphs():
    """整栏宽的长段落仍要判成正文，防止 find_tables 把正文误当表格。"""
    from paperead.layout import Block, _table_swallows_page

    rect = pymupdf.Rect(60, 50, 540, 700)
    blocks = [
        Block(id=f"s{i}", order=0, page=1, kind="para",
              text="short line", bbox=(70, 60 + i * 12, 300, 70 + i * 12))
        for i in range(5)
    ]
    blocks += [
        Block(id="p1", order=0, page=1, kind="para",
              text="body paragraph " * 40, bbox=(60, 300, 540, 500)),
        Block(id="p2", order=0, page=1, kind="para",
              text="another paragraph " * 40, bbox=(60, 520, 540, 560)),
    ]
    assert _table_swallows_page(1, rect, blocks) is True


def test_figure_rect_is_trimmed_off_body_text():
    """图区被整圈边框扩到页顶时，要收回到正文段落之外。"""
    from paperead.layout import Block, _trim_figure_rect_off_body

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    rect = pymupdf.Rect(50, 60, 560, 430)
    body = Block(
        id="body", order=0, page=1, kind="para", size=10.0,
        text="section body " * 60, bbox=(90, 95, 520, 170),
    )
    out = _trim_figure_rect_off_body(page, rect, [body], 1, 10.0)
    doc.close()
    assert out.y0 >= 170
    assert out.y1 == pytest.approx(430)


def test_rect_with_adjacent_figure_caption_is_figure_region():
    """顶部大块图形 + 紧邻 Figure 图注，不能当成上一页表格的续表。"""
    from paperead.layout import Block, _rect_is_figure_region

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(
        pymupdf.Rect(100, 100, 500, 400), color=(0, 0, 0), fill=(0.95, 0.95, 0.95)
    )
    for i in range(4):
        page.draw_line((110, 140 + i * 60), (490, 140 + i * 60), color=(0, 0, 0))
    cap = Block(
        id="c", order=0, page=1, kind="caption",
        text="Figure 1: demo", bbox=(100, 410, 500, 420),
    )
    hit = _rect_is_figure_region(page, pymupdf.Rect(90, 90, 510, 400), [cap], 1)
    doc.close()
    assert hit is True
