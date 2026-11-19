"""首页前置区（标题/多列作者卡片/摘要/关键词）回归测试。

对应缺陷：IEEE 等版式的首页是「通栏标题 + 三列作者卡片 + 左栏摘要 +
右栏正文」的混合版式。用正文的 gutter 去排序，中间列作者会被当成跨栏
锚点插到左右列前面；摘要正文是粗体，又被当作一行一个章节标题。这里用
最小 Line/Block 序列固定这三条行为。
"""

from __future__ import annotations

from paperead.layout import (
    Block,
    FrontMatter,
    Line,
    _detect_front_matter,
    _looks_author_name,
    _merge_front_matter_cards,
    _reading_order,
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


def test_author_name_detection_covers_common_forms():
    assert _looks_author_name("1st Zhen Gao")
    assert _looks_author_name("5rd Ju Xiang")
    assert _looks_author_name("Kodur Sai Vinay Sathvik2 and Y. Narahari1")
    assert _looks_author_name(
        "Qi Xuan, Senior Member, IEEE, Shirui Pan, Senior Member, IEEE, Xin Zheng"
    )
    assert _looks_author_name("张三")
    assert not _looks_author_name("School of Computer Science and Technology")
    assert not _looks_author_name("Changsha, China")
    assert not _looks_author_name("Technology")
    assert not _looks_author_name("gaozhenchn@163.com")
    assert not _looks_author_name("R E V I E W")
    assert not _looks_author_name("Received: 20 March 2025")


def _front_lines() -> list[Line]:
    lines = [
        _line("A Signed Directed Graph Transformer", 60, 50, 552, size=24, col=-1),
        _line("1st Zhen Gao", 95, 150, 156, size=9, col=0),
        _line("2nd Jie Jiang", 275, 150, 330, size=9, col=-1),
        _line("3rd Nengwei Deng", 450, 150, 519, size=9, col=1),
        _line("School of Computer Science", 47, 162, 209, size=9, col=0),
        _line("School of Computer Science", 225, 162, 387, size=9, col=-1),
        _line("School of Computer Science", 402, 162, 564, size=9, col=1),
        _line("4th Anqi Liu", 85, 235, 196, size=9, col=0),
        _line("School of Computer Science", 358, 248, 520, size=9, col=1),
        # 摘要：粗体 9pt、行尾没有标点（旧逻辑会逐行判 heading）
        _line("Abstract—Fine-grained gene regulatory network prediction", 58, 340, 290, size=9, bold=True),
        _line("distinguishes no-edge and activation relationships while", 44, 350, 290, size=9, bold=True),
        _line("jointly modeling direction and signs remains challenging", 44, 360, 290, size=9, bold=True),
        _line("and multi-hop dependencies require a signed directed model", 44, 370, 290, size=9, bold=True),
        _line("Keywords—gene regulatory network, graph transformer", 58, 420, 290, size=9, bold=True),
        _line("I. INTRODUCTION", 127, 460, 206, size=10, col=0),
        # 右栏是正文：与摘要同 y，但 col=1
        _line("Graph neural network methods also exploit topology while", 330, 340, 570, col=1),
        _line("graph transformers offer attention-based structural modeling", 330, 350, 570, col=1),
    ]
    return lines


def test_front_matter_detects_head_abstract_and_keywords():
    front = _detect_front_matter(_front_lines(), body_size=10.0, page_height=792, page_width=612)
    assert front.page == 1
    assert front.head_end == 340
    assert front.abstract == (340, 420)
    assert front.keywords == (420, 460)
    assert front.abstract_col == 0
    assert front.in_abstract(_line("distinguishes no-edge", 44, 352, 290, size=9, bold=True))
    # 右栏正文与摘要同 y，但不属于摘要
    assert not front.in_abstract(
        _line("Graph neural network methods", 330, 352, 570, col=1)
    )
    assert front.in_head((95, 150, 156, 160))


def test_three_column_authors_keep_row_order():
    lines = _front_lines()
    front = _detect_front_matter(lines, body_size=10.0, page_height=792, page_width=612)
    ordered = _reading_order(lines, 0.0, front, page_width=612)
    names = [
        ln.text for ln in ordered
        if ln.text.startswith(("1st ", "2nd ", "3rd ", "4th "))
    ]
    assert names == [
        "1st Zhen Gao",
        "2nd Jie Jiang",
        "3rd Nengwei Deng",
        "4th Anqi Liu",
    ]


def test_front_matter_cards_merge_per_column():
    front = FrontMatter(page=1, head_end=340)
    blocks = [
        Block(id="", order=0, page=1, kind="author", text="1st Zhen Gao", bbox=(95, 150, 156, 160)),
        Block(id="", order=1, page=1, kind="author", text="2nd Jie Jiang", bbox=(275, 150, 330, 160)),
        Block(id="", order=2, page=1, kind="author", text="3rd Nengwei Deng", bbox=(450, 150, 519, 160)),
        Block(id="", order=3, page=1, kind="affiliation", text="School of Computer Science", bbox=(47, 162, 209, 172)),
        Block(id="", order=4, page=1, kind="author", text="4th Anqi Liu", bbox=(85, 235, 196, 245)),
        Block(id="", order=5, page=1, kind="affiliation", text="International College of Engineering", bbox=(85, 246, 262, 256)),
    ]
    merged = _merge_front_matter_cards(blocks, front, page_width=612)
    # 1st 卡片 = 姓名 + 单位；4th 是另一张卡片，不能和 1st 粘在一起
    assert [b.kind for b in merged] == ["author", "author", "author", "author"]
    assert merged[0].text.startswith("1st Zhen Gao")
    assert "School of Computer Science" in merged[0].text
    assert "4th" not in merged[0].text
    assert merged[3].text.startswith("4th Anqi Liu")
    assert "International College of Engineering" in merged[3].text