"""侧栏原地翻译：机构/期刊/citation 可译，联系方式与标识白名单。"""

from __future__ import annotations

from paperead.layout import (
    Block,
    Line,
    _is_sidebar_whitelist,
    _split_cross_column_blocks,
)


def _line(text: str, x0: float, x1: float, y0: float) -> Line:
    return Line(
        text=text,
        bbox=(x0, y0, x1, y0 + 10),
        page=1,
        size=10,
        col=0,
        baseline=y0 + 8,
    )


def test_sidebar_whitelist_rules() -> None:
    assert _is_sidebar_whitelist("germano.cneto@gmail.com") is True
    assert _is_sidebar_whitelist("orcid.org/0000-0001-9429-5855") is True
    assert _is_sidebar_whitelist("doi: 10.3389/fpls.2021.717552") is True
    assert _is_sidebar_whitelist("31 May 2021") is False
    assert _is_sidebar_whitelist("Department of Genetics, ESALQ/USP") is False
    assert _is_sidebar_whitelist("Frontiers in Plant Science") is False


def test_sidebar_affiliation_stays_translatable() -> None:
    left = [
        _line("Department of Oncology, Boston, MA, USA", 60, 160, 100),
        _line("Frontiers in Plant Science", 80, 160, 114),
        _line("Received: 31 May 2021", 82, 160, 128),
    ]
    right = [_line("Right body sentence one.", 180, 550, 100)]
    text = (
        "Department of Oncology, Boston, MA, USA Frontiers in Plant Science "
        "Received: 31 May 2021 Right body sentence one."
    )
    block = Block(
        id="b0000", order=0, page=1, kind="para", text=text,
        bbox=(60, 100, 550, 138),
    )
    out = _split_cross_column_blocks(
        [block], left + right, {1: None}, 600.0, {1: [(0.0, 160.5)]}
    )

    assert len(out) == 2
    side, body = out
    assert side.column_role == "sidebar"
    # 机构/期刊/日期不是白名单：侧栏要原地翻译。
    assert side.translatable is True
    assert body.translatable is True