"""参考文献与正文引用识别的回归测试。

对应缺陷：参考文献页顶部的页码被判成 heading，结束「参考文献状态」；
`[22] Author...` 这种方括号条目又无法重新进入该状态（旧正则只认
`22.`/`(22)`），导致跨页之后整段参考文献变成正文，正文引用全部对不上。
"""

from __future__ import annotations

import pymupdf

from paperead.layout import Block, _looks_reference_entry, parse_pdf
from paperead.tutor.references import _clean_title, _split_entries, parse as parse_references


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