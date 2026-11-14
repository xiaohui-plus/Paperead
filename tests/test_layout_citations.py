"""参考文献与正文引用识别的回归测试。

对应缺陷：参考文献页顶部的页码被判成 heading，结束「参考文献状态」；
`[22] Author...` 这种方括号条目又无法重新进入该状态（旧正则只认
`22.`/`(22)`），导致跨页之后整段参考文献变成正文，正文引用全部对不上。
"""

from __future__ import annotations

import pymupdf

from paperead.layout import (
    Block,
    _aligns_column_edge,
    _looks_reference_entry,
    _split_eq_number,
    parse_pdf,
)
from paperead.tutor.references import (
    _author_names,
    _clean_title,
    _extract_year,
    _fallback_reference_blocks,
    _good_title,
    _is_continuation,
    _looks_author_year,
    _only_reference_section,
    _pieces,
    _segment_score,
    _split_entries,
    _split_numbered,
    _year_suffix,
    parse as parse_references,
)


def _pdf(path, pages):
    doc = pymupdf.open()
    for draw in pages:
        page = doc.new_page(width=612, height=792)
        draw(page)
    doc.save(str(path))
    doc.close()
    return path


def _body(page, y0=110):
    for i in range(4):
        page.insert_text(
            (60, y0 + i * 14),
            "Body paragraph line with enough words for body size estimation.",
            fontsize=10,
        )


def _ref(page, text, y, size=9):
    page.insert_text((60, y), text, fontsize=size)


def test_bracket_references_survive_page_number_heading(tmp_path):
    def page_one(page):
        _body(page)
        page.insert_text((60, 220), "References", fontsize=12, fontname="hebo")
        _ref(page, "[1] Smith, J. 2020. A first study of citation parsing. Journal of Tests, 12(3):101-110.", 250)
        _ref(page, "[2] Doe, A. and Roe, B. 2021. A second study of citation parsing. arXiv preprint arXiv:2101.00001.", 270)

    def page_two(page):
        # 参考文献页顶部页码：字号大、又不在 7.5% 页眉带内，会被判成 heading
        page.insert_text((290, 60), "2", fontsize=14, fontname="hebo")
        _ref(page, "[3] Poe, C. 2022. A third study of citation parsing. Journal of Tests, 13(1):1-9.", 110)
        _ref(page, "[4] Moe, D. 2023. A fourth study of citation parsing. arXiv preprint arXiv:2301.00002.", 130)

    pdf = _pdf(tmp_path / "refs.pdf", [page_one, page_two])
    parsed = parse_pdf(pdf, paper_id="refs", with_figures=False)
    refs = parse_references(parsed.blocks)
    indices = [r.index for r in refs]

    assert indices == [1, 2, 3, 4], [r.to_dict() for r in refs]
    by_index = {r.index: r for r in refs}
    assert by_index[1].title == "A first study of citation parsing"
    assert by_index[3].title == "A third study of citation parsing"
    # 页码本身要被当噪声丢掉，不能留在阅读流里
    assert not any(b.text.strip() == "2" for b in parsed.blocks)


def test_bracket_number_is_a_reference_entry():
    assert _looks_reference_entry(
        "[22] Wenhu Chen, Ming Yin, Max Ku, Pan Lu, Yixin Wan. 2025. Reward modeling as reasoning. arXiv preprint arXiv:2505.00001."
    )
    assert not _looks_reference_entry("[22]")


def test_multiple_bracket_entries_in_one_block():
    block = Block(
        "b0", 0, 1, "reference",
        "[1] A. Author. 2020. First title with enough words. [2] B. Author. 2021. Second title with enough words.",
        (60, 100, 540, 140),
    )
    entries = _split_entries([block])
    assert [num for num, _ in entries] == [1, 2]


def test_parse_keeps_entry_when_title_is_weak():
    # 标题抽不出来（只剩 URL）时不能整条丢弃，否则正文引用没有落点
    block = Block("b0", 0, 1, "reference", "1. https://example.org/only-url 2020.", (60, 100, 540, 120))
    refs = parse_references([block])
    assert [r.index for r in refs] == [1]


def test_bracket_year_yields_real_title():
    text = (
        "1.\t Wang H, Fu T, Du Y, Gao W, Huang K, Liu Z, et al (2023) "
        "Scientific discovery in the era of artificial intelligence. Nature, 625:1-10."
    )
    title = _clean_title(text)
    assert title.startswith("Scientific discovery in the era of artificial intelligence")

# ── 参考文献作者区与年份后缀 ──────────────────────────


def test_author_names_recovers_semicolon_fragments():
    raw = "Lada; A; Adamic; ; ; Eytan; and Adar. 2003. Information flow in social groups."
    names = _author_names(raw)
    assert names == ["Lada", "Adamic", "Eytan", "Adar"]


def test_author_names_drops_venue_words_and_stops_at_ten():
    raw = "John Smith, Jane Doe, Neural Networks, Proceedings of the Conference, 2020. Title."
    names = _author_names(raw)
    assert names[:2] == ["John", "Smith"]
    assert "Neural" not in names
    assert "Proceedings" not in names

    long_raw = "Alice Bob, " + " ".join(f"Name{i}" for i in range(20)) + " 2021. Title"
    assert len(_author_names(long_raw)) == 10


def test_year_suffix_reads_letter_after_year():
    assert _year_suffix("Kipf and Welling 2016b. Title", 2016) == "b"
    assert _year_suffix("Kipf and Welling 2016. Title", 2016) == ""
    assert _year_suffix("Kipf and Welling 2016b", None) == ""


def test_semicolon_author_list_is_not_a_continuation():
    # 分号作者串要单独成条，否则 `Lada et al. 2003` 永远没有落点
    assert not _is_continuation("Lada; A; Adamic; ; ; Eytan", "Some previous entry text")
    assert _is_continuation("continued text without a leading capital list", "Some previous entry")
    assert _is_continuation("works and more", "A title that ends with a hy-")


def test_parse_populates_authors_and_year_suffix():
    block = Block(
        "b0",
        0,
        1,
        "reference",
        "Kipf, T. N., and Welling, M. 2016b. Semi-supervised classification with "
        "graph convolutional networks. ICLR.",
        (60, 100, 540, 140),
    )
    refs = parse_references([block])
    assert len(refs) == 1
    assert refs[0].year == 2016
    assert refs[0].suffix == "b"
    assert refs[0].authors[:2] == ["Kipf", "Welling"]
    assert refs[0].to_dict()["suffix"] == "b"
    assert refs[0].to_dict()["authors"][:2] == ["Kipf", "Welling"]


# ── 公式编号：栏右缘对齐与行内编号 ────────────────────


def test_aligns_column_edge_uses_nearest_right_edge():
    xs = [300.0, 532.0]
    # 双栏左栏/右栏的编号贴栏右缘，容差 5pt
    assert _aligns_column_edge(xs, 534.0)
    assert not _aligns_column_edge(xs, 400.0)
    assert not _aligns_column_edge([], 100.0)


def test_split_eq_number_extracts_trailing_number():
    body, num = _split_eq_number("a^2 + b^2 = c^2 (3)")
    assert body == "a^2 + b^2 = c^2"
    assert num == "3"


def test_split_eq_number_extracts_inline_number_between_scripts():
    body, num = _split_eq_number("aij = ... (9) j + bij d")
    assert num == "9"
    assert "(9)" not in body
    assert "aij" in body
    assert "j + bij d" in body


def test_split_eq_number_keeps_function_call_parentheses():
    # `f(9)` 是数学内容，不能当成公式编号删掉
    body, num = _split_eq_number("f(9) is the activation")
    assert num == ""
    assert body == "f(9) is the activation"

# ── 参考文献切分与降级路径 ────────────────────────────


def _block(idx, text, kind="reference", page=1, section=""):
    block = Block(f"b{idx}", idx, page, kind, text, (60, 100, 540, 140))
    block.section = section
    return block


def test_pieces_returns_empty_for_blank_text():
    assert _pieces("   ") == []
    assert _pieces("[1] First. [2] Second.") == ["[1] First.", "[2] Second."]


def test_looks_author_year_covers_prefix_and_rejection_paths():
    assert _looks_author_year("Kipf, T. 2016. Tabular data analysis.")
    assert _looks_author_year("Smith and Doe 2020. A title here")
    assert _looks_author_year("Zhang et al. 2018. Deep residual learning.")
    assert not _looks_author_year("no year prefix in this piece at all")
    assert not _looks_author_year("Kipf, T. 2016. Short")
    assert not _looks_author_year("lower case author 2020. Title words here")
    assert not _looks_author_year("A" * 170 + " 2020. Title words here")


def test_is_continuation_requires_previous_piece():
    assert not _is_continuation("anything", None)


def test_fallback_reference_blocks_uses_references_heading():
    blocks = [_block(0, "References", kind="para")]
    for i in range(5):
        blocks.append(
            _block(
                i + 1,
                f"[{i + 1}] Author {i} 2020. A sufficiently long reference title.",
                kind="para",
            )
        )
    fallback = _fallback_reference_blocks(blocks)
    assert len(fallback) == 5
    assert fallback[0].startswith("[1]")


def test_fallback_reference_blocks_requires_pages_and_heading():
    # page=0 是无效页码，页集合为空时直接放弃
    assert _fallback_reference_blocks(
        [_block(0, "Body text long enough to be content.", kind="para", page=0)]
    ) == []

    blocks = [
        _block(0, "Body text long enough to be real content.", kind="para", page=1),
        _block(1, "[1] Author 2020. A long enough reference entry for the last page.", kind="para", page=2),
        _block(2, "[2] Author 2021. Another long enough reference entry here.", kind="para", page=2),
    ]
    fallback = _fallback_reference_blocks(blocks)
    assert len(fallback) == 2
    assert fallback[1].startswith("[2]")


def test_only_reference_section_prefers_complete_canonical_list():
    blocks = [
        _block(
            i,
            f"[{i + 1}] A. Author 2020. A long enough reference entry title number {i}.",
            section="References",
        )
        for i in range(5)
    ]
    fallback = [b.text for b in blocks]
    assert _only_reference_section(blocks, fallback) == fallback


def test_split_numbered_handles_zero_jump_and_no_numbers():
    assert _split_numbered("plain text without numbers", None) == ["plain text without numbers"]
    assert _split_numbered("0. invalid 1. valid entry", None) == ["0. invalid", "1. valid entry"]
    assert _split_numbered("1. first 2. second", None) == ["1. first", "2. second"]
    # 突跳编号不切条（卷号/年份会被误认）
    assert _split_numbered("1. first 50. jumped", None) == ["1. first 50. jumped"]


def test_split_entries_falls_back_when_reference_blocks_missing():
    blocks = [_block(0, "References", kind="para")]
    for i in range(5):
        blocks.append(
            _block(
                i + 1,
                f"[{i + 1}] Author {i} 2020. A sufficiently long reference title.",
                kind="para",
            )
        )
    entries = _split_entries(blocks)
    assert [num for num, _ in entries] == [1, 2, 3, 4, 5]


def test_split_entries_prefers_longer_fallback():
    blocks = [
        _block(0, "[1] A. Author 2020. A long enough reference entry title here."),
        _block(1, "References", kind="para"),
    ]
    # 标题后至少 5 条才会启用 fallback（不足 5 条时视为误判标题）
    for i in range(1, 6):
        blocks.append(
            _block(
                i + 1,
                f"[{i + 1}] B. Author 2021. Another long enough reference entry {i}.",
                kind="para",
            )
        )
    entries = _split_entries(blocks)
    assert [num for num, _ in entries] == [2, 3, 4, 5, 6]


def test_split_entries_filters_zero_jump_and_regressed_numbers():
    text = (
        "[1] A. Author 2020. First entry with enough words here. "
        "[0] Z. Author 2023. Zero numbered entry with enough words. "
        "[40] B. Author 2021. Jumped entry with enough words here. "
        "[2] C. Author 2022. Regressed entry with enough words here."
    )
    entries = _split_entries([_block(0, text)])
    assert [num for num, _ in entries] == [1, 2]
    joined = " ".join(t for _, t in entries)
    assert "Jumped entry" not in joined
    assert "Zero numbered" not in joined


def test_split_entries_starts_new_entry_for_author_year_piece():
    text = (
        "[1] Kipf, T. 2016. First entry with enough words here. "
        "Smith, J. 2020. Second author-year entry with enough words."
    )
    entries = _split_entries([_block(0, text)])
    assert [num for num, _ in entries] == [1, None]
    assert entries[1][1].startswith("Smith, J. 2020.")


def test_split_entries_joins_hyphenated_continuation():
    blocks = [
        _block(0, "Graph con-", section="References"),
        _block(1, "vnet models improve accuracy substantially.", section="References"),
    ]
    entries = _split_entries(blocks)
    assert entries == [(None, "Graph convnet models improve accuracy substantially.")]


def test_split_entries_keeps_word_boundary_for_uppercase_fragment():
    blocks = [
        _block(0, "A long title about neural net-", section="References"),
        _block(1, "Works improve accuracy substantially.", section="References"),
    ]
    entries = _split_entries(blocks)
    assert entries == [
        (None, "A long title about neural net-Works improve accuracy substantially.")
    ]


def test_split_entries_appends_unparseable_piece_in_numbered_mode():
    # 编号模式下，既不是新编号也不是作者-年份的片段并入上一条，而不是丢掉
    blocks = [
        _block(0, "[1] A. Author 2020. First entry with enough words here.", section="References"),
        _block(1, "and the entry continues without an author-year prefix here.", section="References"),
    ]
    entries = _split_entries(blocks)
    assert len(entries) == 1
    assert entries[0][0] == 1
    assert "continues without an author-year prefix" in entries[0][1]


def test_split_entries_joins_plain_continuation_with_space():
    blocks = [
        _block(0, "A long reference entry title without a number.", section="References"),
        _block(1, "continued words that belong to the same entry.", section="References"),
    ]
    entries = _split_entries(blocks)
    assert entries == [
        (
            None,
            "A long reference entry title without a number. "
            "continued words that belong to the same entry.",
        )
    ]


def test_parse_skips_short_entries():
    assert parse_references([_block(0, "[1] Tiny.")]) == []


def test_parse_assigns_sequential_index_avoiding_printed_numbers():
    text = (
        "[2] A. Author 2020. First entry with enough words to pass the length gate. "
        "Smith, J. 2020. Second entry with enough words here to pass the length gate."
    )
    refs = parse_references([_block(0, text)])
    assert [r.index for r in refs] == [2, 3]


def test_parse_skips_duplicate_printed_numbers():
    blocks = [
        _block(0, "[1] A. Author 2020. First entry with enough words to pass the gate."),
        _block(1, "[1] B. Author 2021. Duplicate numbered entry with enough words here."),
    ]
    refs = parse_references(blocks)
    assert [r.index for r in refs] == [1]
    assert refs[0].raw.startswith("[1] A. Author")


def test_good_title_rejects_short_one_word_upper_and_links():
    assert not _good_title("abcd")
    assert not _good_title("Oneword")
    assert not _good_title("ALL CAPS TITLE")
    assert not _good_title("See https://example.org for details")
    assert _good_title("A good title here")


def test_segment_score_penalises_pieces_without_words():
    assert _segment_score("123 456 789") == -1e9


def test_clean_title_prefers_quoted_title():
    assert _clean_title('"A Quoted Study Title" and some trailing venue text.') == (
        "A Quoted Study Title"
    )


def test_extract_year_reads_quoted_and_parenthesised_years():
    assert _extract_year('"A Quoted Study Title" Journal of Tests, 2021.') == 2021
    assert _extract_year("Wang H, Fu T, et al (2023) Scientific discovery in AI.") == 2023