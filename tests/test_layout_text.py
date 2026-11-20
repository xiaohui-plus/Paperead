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
    _absorb_table_notes,
    _build_blocks,
    _caption_continuation,
    _collect_rotated_caption_blocks,
    _drop_noise,
    _is_caption_text,
    _is_heading,
    _is_table_caption,
    _join_text,
    _line_from_dict,
    _looks_like_text_table,
    _merge_across_pages,
    _merge_caption_continuations,
    _merge_math_fragments,
    _merge_small_caption_continuations,
    _merge_visual_rows,
    _normalize_text,
    _split_row_runs,
    _table_text_rotation,
    parse_pdf,
)
from paperead.inline_math import build_inline_math, spans_with_offsets
from paperead.textnorm import is_dot_leader_only, squeeze_cjk_spaces, strip_dot_leaders


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


def test_cjk_lines_join_without_inserted_space():
    # 中文换行是排版折行，不是词间空格
    assert _join_text("中文句子在这一行结束", "下一行继续") == "中文句子在这一行结束下一行继续"
    assert _join_text("句子结束。", "下一句开始") == "句子结束。下一句开始"
    # 中英混排仍按西文规则留空格
    assert _join_text("使用 Transformer", "模型完成推理") == "使用 Transformer 模型完成推理"


def test_cjk_spaced_text_layer_is_squeezed():
    text, mapping = squeeze_cjk_spaces("中 文 文 献 版 面 重 排")
    assert text == "中文文献版面重排"
    # 偏移映射必须指到清理后的同一位置，行内公式才不会错位
    assert [text[mapping[i]] for i in (0, 2, 4, 6)] == ["中", "文", "文", "献"]
    assert squeeze_cjk_spaces("使用 Transformer 模型")[0] == "使用 Transformer 模型"
    assert squeeze_cjk_spaces("中文 and 英文")[0] == "中文 and 英文"


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

# ── 目录点线引导符 ─────────────────────────────────────────────────────
def test_dot_leaders_are_stripped_from_toc_entries():
    # PDF 抽出来的点线常被按字距补成 ". . . . ."，清理后标题与页码要保留
    assert (
        strip_dot_leaders("5.2.2 Joint Multimodal Generation . . . . . 20")
        == "5.2.2 Joint Multimodal Generation 20"
    )
    assert strip_dot_leaders("A.1 Evaluation tasks....17") == "A.1 Evaluation tasks 17"
    assert strip_dot_leaders("S1 Docking protocols…… 12") == "S1 Docking protocols 12"
    # 正文里的省略号是标点，不能被当成引导符删掉
    assert strip_dot_leaders("wait... what?") == "wait... what?"


def test_dot_leader_only_block_is_detected():
    assert is_dot_leader_only(". . . . . . . . . . . .")
    assert is_dot_leader_only("• . . . . . . .")
    assert not is_dot_leader_only("5.2.2 Joint Multimodal Generation 20")


def test_drop_noise_removes_dot_leader_blocks_and_keeps_titles():
    blocks = [
        Block("b0", 0, 1, "para", ". . . . . . . . . . . .", (0, 0, 1, 1)),
        Block("b1", 1, 1, "heading", "5.2.2 Joint Multimodal Generation . . . . . 20", (0, 0, 1, 1)),
        Block("b2", 2, 1, "para", "Body paragraph.", (0, 0, 1, 1)),
    ]
    out = _drop_noise(blocks)
    assert [b.text for b in out] == ["5.2.2 Joint Multimodal Generation 20", "Body paragraph."]
    assert [b.id for b in out] == ["b0000", "b0001"]

# ── 行内公式的集合符号与跨行碎片 ───────────────────────────────────────
def _raw_span(text, font, size, x0, x1, y):
    return {
        "text": text,
        "font": font,
        "size": size,
        "bbox": [x0, y - 8, x1, y + 2],
        "origin": [x0, y],
    }


def test_cmex_brace_pieces_are_restored():
    # CMEX10 把 { / } 画成 0x08 / 0x09；0x09 是制表符，不能先按空白过滤掉
    raw = {
        "bbox": [100, 400, 200, 415],
        "dir": (1, 0),
        "spans": [
            _raw_span("x", "CMMIB10", 10, 100, 106, 410),
            _raw_span("\x08", "CMEX10", 10, 108, 112, 409),
            _raw_span("t", "CMMI7", 7, 112, 115, 413),
            _raw_span("\x09", "CMEX10", 10, 116, 120, 409),
        ],
    }
    ln = _line_from_dict(raw, 1)
    assert ln is not None
    assert ln.brace_piece
    assert "{" in ln.text and "}" in ln.text
    assert "\x08" not in ln.text and "\x09" not in ln.text


def test_brace_piece_joins_formula_row_by_bbox():
    # 大括号片段 bbox 跨两行、基线落在上一行；必须按 bbox 归到公式行，
    # 否则 `{` 会跑到上一段正文末尾
    body = _line("For visual imagination supervision ... observation", 108, 504, baseline=415.6)
    formula = _line("xt ∈", 108, 127.5, baseline=426.5)
    brace = Line(
        text="{",
        bbox=(130.2, 417.9, 136.1, 427.8),
        page=1,
        size=10.0,
        baseline=418.5,
        brace_piece=True,
    )
    rows = _merge_visual_rows([body, formula, brace], {})
    row = next(r for r in rows if "{" in r.text)
    assert row.baseline == 426.5


def test_formula_keeps_prose_word_between_math_spans():
    # x^front 的 front 用正文正体排版，夹在两个数学 span 之间，不能把公式切断
    spans = [
        _raw_span("x", "CMMIB10", 10, 100, 106, 410),
        _raw_span("front", "NimbusRomNo9L-Regu", 7, 106, 130, 406),
        _raw_span("t", "CMMI7", 7, 130, 133, 413),
    ]
    text, ranges = spans_with_offsets(spans)
    out = build_inline_math(spans, ranges)
    assert len(out) == 1
    assert "front" in out[0].source


def test_math_fragments_merge_and_fallback():
    text = "xt front t"
    frags = [
        {
            "start": 0, "end": 2, "latex": "x_{t}", "fallback": False,
            "source": "xt", "bbox": [100, 400, 110, 410],
        },
        {
            "start": 9, "end": 10, "latex": "t", "fallback": False,
            "source": "t", "bbox": [130, 400, 136, 410],
        },
    ]
    merged = _merge_math_fragments(text, frags)
    assert len(merged) == 1
    assert merged[0]["fallback"] is True
    assert merged[0]["start"] == 0 and merged[0]["end"] == 10

# ── 首页窄摘要不应被同页更宽的行切成一行一段 ───────────────────────────
def _body_line(text, x0, x1, y0, size=10.0, page=1, col=0):
    return Line(
        text=text,
        bbox=(x0, y0 - 8, x1, y0 + 2),
        page=page,
        size=size,
        col=col,
        baseline=y0,
    )


def _abstract_lines():
    lines = [
        "Vision language navigation is very challenging for unmanned aerial vehicles since",
        "small errors in 3D motion prediction can lead to navigation errors and collisions.",
        "Unable to fathom execution outcomes in 3D environments, methods struggle to",
        "predict accurate navigation waypoints from language instructions. We introduce",
        "a framework that imagines the visual representation of the intended next movement",
        "and then uses the predicted prototypes as context for generating the final action.",
        "Data and code are available at the project page.",
    ]
    return [
        _body_line(text, 143.5, 469.8 if i < 6 else 438.8, 266.0 + i * 11)
        for i, text in enumerate(lines)
    ]


def test_narrow_abstract_is_not_split_by_wider_page_lines():
    # 首页标题/页脚比摘要宽，整页边界会被撑到 60..552；局部宽度估计不应受影响
    wide = _body_line(
        "A wide front matter line that spans the entire page width", 60, 552, 60, size=16
    )
    heading = _body_line("1 Introduction", 143.5, 260, 266.0 + 7 * 11, size=14)
    blocks = _build_blocks([wide, *_abstract_lines(), heading], 10.0)

    abstract = [b for b in blocks if "Vision language navigation" in b.text]
    assert len(abstract) == 1
    assert "Data and code are available" in abstract[0].text
    assert "final action" in abstract[0].text


def test_real_paragraph_break_survives_short_last_line():
    # 上一段末行确实短、下一段首字母大写：仍应分成两块，不能被过度合并
    first = [
        _body_line(
            "First paragraph line one is full width and carries enough words to fill the column",
            143.5, 469.8, 100,
        ),
        _body_line(
            "second line is also full width and continues the discussion of the proposed method",
            143.5, 469.8, 111,
        ),
        _body_line("the paragraph ends here.", 143.5, 350.0, 122),
    ]
    second = [
        _body_line(
            "Second paragraph starts with a capital letter and is not a continuation at all",
            143.5, 469.8, 133,
        ),
        _body_line(
            "another full width line in the second paragraph for the geometry check of layout",
            143.5, 469.8, 144,
        ),
    ]
    blocks = _build_blocks([*first, *second], 10.0)
    assert len(blocks) == 2
    assert "the paragraph ends here." in blocks[0].text
    assert blocks[1].text.startswith("Second paragraph")

def test_detected_table_rows_stay_separate_blocks():
    # 表格行行距紧、每行都写满表宽，若按正文合并，兜底切表就拿不到多行短块
    lines = [
        _body_line("DATASET # GRAPHS AVG. # NODES AVG. # EDGES AVG. # RINGS", 143.5, 469.8, 100),
        _body_line("CEPDB 2.3M HOPV 350 PFD 1055 NFA 654 PD 277", 143.5, 469.8, 111),
        _body_line("by inter-level message passing module on GI.", 143.5, 469.8, 122),
    ]
    tables = {1: [pymupdf.Rect(140, 95, 475, 118)]}
    blocks = _build_blocks(lines, 10.0, tables)
    assert [b.text for b in blocks] == [
        "DATASET # GRAPHS AVG. # NODES AVG. # EDGES AVG. # RINGS",
        "CEPDB 2.3M HOPV 350 PFD 1055 NFA 654 PD 277",
        "by inter-level message passing module on GI.",
    ]

# ── 中文图注/表注、小标题与跨页续段 ───────────────────────────────────
def test_chinese_table_caption_is_recognized():
    assert _is_caption_text("表2 数据治理前后核心指标对比表")
    assert _is_caption_text("图3 复合问答—识别材料属性并作出节能判断")
    assert _is_table_caption("表1 数据治理前后核心指标对比表")
    # 正文里的引用不是图注/表注
    assert not _is_caption_text("如表 2 所示，本平台的准确率更高")
    assert not _is_caption_text("图 3 展示系统能够基于复合场景多类信息进行")
    assert not _is_caption_text("表2所示")


def test_chinese_section_heading_is_recognized():
    heading = Line("2.2 智能问答性能", bbox=(56.7, 154.4, 131.7, 167.8), page=4, size=10.0)
    assert _is_heading(heading, 10.0) == 3
    # 表格里的「3.2 分（1～5 分）」是数字+量词，不能当标题
    cell = Line("3.2 分（1～5 分）", bbox=(117.3, 455.2, 252.2, 466.0), page=4, size=8.0)
    assert _is_heading(cell, 10.0) == 0


def test_aastex_plain_font_section_headings_are_recognized():
    """AASTeX 章节标题是正文字号 + 正体（CMR10），旧规则会全部漏成正文。"""
    intro = Line("1. INTRODUCTION", bbox=(124.1, 265.9, 218.8, 275.9), page=2, size=10.0)
    data = Line("2. DATA", bbox=(420.7, 101.9, 461.8, 111.8), page=3, size=10.0)
    subsection = Line(
        "2.1. NIRCam Observations & Data Reduction",
        bbox=(340.0, 203.2, 542.5, 213.2), page=3, size=10.0,
    )
    assert _is_heading(intro, 10.0) == 1
    assert _is_heading(data, 10.0) == 1
    assert _is_heading(subsection, 10.0) == 2
    # 正文引用行以 `T.` 开头，不能被字母编号规则当成附录标题
    citation = Line(
        "T. A. Thompson & T. M. Heckman (2024) and F. R. Donnan",
        bbox=(59.7, 500, 470, 510), page=3, size=10.0,
    )
    assert _is_heading(citation, 10.0) == 0
    # 图内方位/仪器短标签不是标题
    assert _is_heading(Line("N", bbox=(0, 0, 8, 10), page=4, size=10.0), 10.0) == 0
    assert _is_heading(Line("MIRI", bbox=(0, 0, 30, 10), page=4, size=10.0), 10.0) == 0


def test_chinese_paragraph_continues_across_page():
    a = Block(
        "b0", 0, 3, "para",
        "使 BIM 数据的完整性、准确性、一致性及动态更新率",
        (56.7, 600.0, 299.9, 620.0),
    )
    b = Block(
        "b1", 1, 4, "para",
        "等方面得到了极大的提升，主要得益于构件统一分类。",
        (56.7, 80.0, 299.9, 100.0),
    )
    merged = _merge_across_pages([a, b])
    assert len(merged) == 1
    assert "等方面得到了极大的提升" in merged[0].text


def test_caption_does_not_swallow_following_chinese_body():
    cap = Block(
        "b0", 0, 3, "caption",
        "图2 智能问答技术流程图",
        (126.3, 520.0, 225.3, 532.1),
    )
    body = Block(
        "b1", 1, 3, "para",
        "系统在 Prompt 中仅保留角色定义、元数据与用户问题三个部分，"
        "并将结构化结果交给模型生成最终回答，从而减少无关上下文。",
        (56.7, 534.0, 299.9, 560.0),
    )
    out = _merge_caption_continuations([cap, body])
    assert len(out) == 2
    assert out[1].kind == "para"

    # 下一行又是另一处「图 3」引用：不是当前图注的续行
    assert not _caption_continuation(
        "图4 对问题以可视化形式问答",
        "图 3 展示系统能够基于复合场景多类信息进行",
    )


def test_table_rows_are_not_merged_into_caption():
    cap = Block(
        "b0", 0, 4, "caption",
        "表2 数据治理前后核心指标对比表",
        (108.3, 349.4, 243.3, 361.5),
    )
    row = Block(
        "b1", 1, 4, "para",
        "问答准确率 55% 90% 64%",
        (63.9, 390.9, 283.2, 401.7),
    )
    tables = {4: [pymupdf.Rect(60.0, 369.2, 291.9, 464.8)]}
    out = _merge_caption_continuations([cap, row], tables)
    assert len(out) == 2
    assert out[1].text == "问答准确率 55% 90% 64%"

    merged = _merge_across_pages(
        [
            Block("b0", 0, 4, "para", "指标提升幅度", (75.9, 373.6, 291.9, 382.9)),
            Block("b1", 1, 4, "para", "（未治理数据）（治理后数据）", (118.3, 378.4, 247.2, 387.7)),
        ],
        tables,
    )
    assert len(merged) == 2

def test_single_word_and_declaration_headings_are_recognized():
    # BMC 的三级标题 `Simulations` 只有 9.2pt + 粗体
    sim = Line("Simulations", bbox=(117.6, 86.5, 164.7, 97.8), page=7, size=9.2, bold=True)
    assert _is_heading(sim, 9.8) == 3
    # 后置声明区标题常常不加粗、与正文同字号
    fund = Line("Funding", bbox=(117.6, 349.7, 144.6, 358.8), page=16, size=9.8)
    assert _is_heading(fund, 9.8) == 3
    # 图内 8pt 重复标签不是标题
    task = Line("Task Task", bbox=(0, 0, 60, 10), page=4, size=8.0, bold=True)
    assert _is_heading(task, 9.8) == 0


def test_rotated_table_caption_is_collected(tmp_path):
    def draw(page):
        page.insert_text(
            (300, 100),
            "Table 3 The predictive ability of four basic methods",
            fontsize=10,
            rotate=90,
        )
        page.insert_text((280, 100), "0.8879", fontsize=8, rotate=90)

    pdf = _pdf(tmp_path / "rotated-table.pdf", [(612, 792, draw)])
    doc = pymupdf.open(pdf)
    caps = _collect_rotated_caption_blocks(doc)
    assert len(caps) == 1
    assert caps[0].text.startswith("Table 3")
    assert _table_text_rotation(doc[0], pymupdf.Rect(0, 0, 612, 792)) in (90, 270)
    doc.close()


def test_many_column_rotated_table_is_not_text_table():
    html = "<table><tr>" + "".join(
        f"<td>0.8879 ± 0.0008 {i}</td>" for i in range(10)
    ) + "</tr></table>"
    assert _looks_like_text_table(html, 10) is False


def test_table_notes_are_absorbed_into_caption():
    cap = Block("c", 0, 1, "caption", "Table 1: demo", (100, 90, 300, 99))
    table = Block(
        "t", 1, 1, "table", "", (100, 100, 400, 300),
        caption="Table 1: demo", translatable=False,
    )
    note = Block(
        "n", 2, 1, "para", "— Represents no explicit result was found in this method",
        (100, 301, 350, 310), size=7.0,
    )
    out = _absorb_table_notes([cap, table, note], 10.0)
    assert len(out) == 2
    assert "— Represents" in out[0].text
    assert "— Represents" in out[1].caption


def test_table_note_does_not_absorb_normal_body_paragraph():
    cap = Block("c", 0, 1, "caption", "Table 1: demo", (100, 90, 300, 99))
    table = Block("t", 1, 1, "table", "", (100, 100, 400, 300), caption="Table 1: demo")
    body = Block(
        "b", 2, 1, "para",
        "This is a normal body paragraph after the table that must stay separate.",
        (100, 301, 400, 320), size=10.0,
    )
    out = _absorb_table_notes([cap, table, body], 10.0)
    assert len(out) == 3
    assert out[2].text.startswith("This is a normal")


def test_short_line_requires_new_paragraph_evidence():
    a = _body_line("the first line is deliberately long but stops short here", 60, 250, 100)
    lower = _body_line("and continues with lowercase words", 60, 300, 112)
    upper = _body_line("New paragraph begins here", 60, 300, 112)
    same = _build_blocks([a, lower], 10.0, {})
    new = _build_blocks([a, upper], 10.0, {})
    assert len(same) == 1
    assert len(new) == 2