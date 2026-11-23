"""首页「左侧信息栏 + 右侧正文」拆分：abstract / affiliation / footnote 也要拆。"""

from __future__ import annotations

from paperead.layout import (
    Block,
    Line,
    _merge_body_paragraph_fragments,
    _split_cross_column_blocks,
    _strip_front_citation_noise,
)

SIDE = {1: [(0.0, 160.5)]}
PAGE_W = 600.0


def _line(text: str, x0: float, x1: float, y0: float, col: int) -> Line:
    return Line(
        text=text,
        bbox=(x0, y0, x1, y0 + 10),
        page=1,
        size=10,
        col=col,
        baseline=y0 + 8,
    )


def _block(kind: str, text: str, bbox, translatable: bool = True, **kw) -> Block:
    return Block(
        id="b0000",
        order=0,
        page=1,
        kind=kind,
        text=text,
        bbox=bbox,
        translatable=translatable,
        **kw,
    )


def test_abstract_side_mix_is_split_and_body_stays_abstract() -> None:
    left = [
        _line("Edited by: Soren K. Rasmussen,", 50, 158, 100, 0),
        _line("University of Copenhagen, Denmark", 50, 158, 114, 0),
        _line("Reviewed by: some reviewer here", 50, 158, 128, 0),
    ]
    right = [
        _line("Quantitative genetics states that phenotypic variation is a consequence", 178, 550, 100, 1),
        _line("of the interaction between genetic and environmental factors.", 178, 550, 114, 1),
        _line("Predictive breeding is based on this statement.", 178, 550, 128, 1),
    ]
    text = " ".join([ln.text for ln in left + right])
    out = _split_cross_column_blocks(
        [_block("abstract", text, (50, 100, 550, 140))],
        left + right,
        {1: None},
        PAGE_W,
        SIDE,
    )

    assert len(out) == 2
    side, body = out
    assert side.column_role == "sidebar" and "Quantitative" not in side.text
    assert body.column_role == "body" and body.kind == "abstract"
    assert body.translatable is True
    assert "Quantitative genetics" in body.text


def test_affiliation_side_mix_body_piece_becomes_para() -> None:
    left = [
        _line("Michael Benjamin Kantar,", 50, 158, 100, 0),
        _line("University of Hawaii, United States", 50, 158, 114, 0),
    ]
    right = [
        _line("information. Here, we present an enviromic assembly approach,", 178, 550, 100, 1),
        _line("which includes using ecophysiology knowledge in shaping relatedness.", 178, 550, 114, 1),
    ]
    text = " ".join([ln.text for ln in left + right])
    out = _split_cross_column_blocks(
        [_block("affiliation", text, (50, 100, 550, 126), translatable=False)],
        left + right,
        {1: None},
        PAGE_W,
        SIDE,
    )

    assert len(out) == 2
    side, body = out
    assert side.column_role == "sidebar" and "information" not in side.text
    # 拆出的正文片段本来是被解析误并进单位的正文，必须恢复为可译段落
    assert body.kind == "para" and body.column_role == "body"
    assert body.translatable is True


def test_footnote_side_mix_body_piece_becomes_para() -> None:
    left = [
        _line("Received: 31 May 2021", 50, 158, 100, 0),
        _line("Accepted: 03 September 2021", 50, 158, 114, 0),
    ]
    right = [
        _line("two types of enviromics assembly, six experimental network sizes,", 178, 550, 100, 1),
        _line("and two types of optimized training set across environments.", 178, 550, 114, 1),
    ]
    text = " ".join([ln.text for ln in left + right])
    out = _split_cross_column_blocks(
        [_block("footnote", text, (50, 100, 550, 126), translatable=False)],
        left + right,
        {1: None},
        PAGE_W,
        SIDE,
    )

    assert len(out) == 2
    body = out[1]
    assert body.kind == "para" and body.translatable is True


def test_single_side_label_does_not_block_split() -> None:
    left = [_line("Citation:", 130, 158, 100, 0)]
    right = [
        _line("benchmark GP in all scenarios, especially when considering", 178, 550, 100, 1),
        _line("smaller training sets and their overall ranking.", 178, 550, 114, 1),
    ]
    text = " ".join([ln.text for ln in left + right])
    out = _split_cross_column_blocks(
        [_block("para", text, (130, 100, 550, 126))],
        left + right,
        {1: None},
        PAGE_W,
        SIDE,
    )

    assert len(out) == 1
    assert out[0].column_role == "body"
    # 信息栏孤立短标签不该留在正文里
    assert "Citation" not in out[0].text
    assert "benchmark GP" in out[0].text


def test_split_fragments_merge_back_into_one_paragraph() -> None:
    first = _block("para", "We observed that E-GP outperforms", (178, 100, 550, 110))
    second = _block("para", "benchmark GP in all scenarios.", (178, 114, 550, 124))

    out = _merge_body_paragraph_fragments([first, second], 10.0)

    assert len(out) == 1
    assert out[0].text.endswith("benchmark GP in all scenarios.")
    assert out[0].bbox[3] == 124


def test_finished_sentence_is_not_merged() -> None:
    first = _block("para", "This paragraph is already complete.", (178, 100, 550, 110))
    second = _block("para", "A new paragraph starts here.", (178, 114, 550, 124))

    out = _merge_body_paragraph_fragments([first, second], 10.0)

    assert len(out) == 2


def test_cross_page_block_is_not_merged() -> None:
    first = _block("para", "The conventional GBLUP is inefficient in", (178, 100, 550, 110))
    crossing = _block(
        "para",
        "predicting unseen environments",
        (178, 114, 550, 124),
        bboxes=[(1, 178, 114, 550, 124), (2, 178, 61, 550, 99)],
    )

    out = _merge_body_paragraph_fragments([first, crossing], 10.0)

    assert len(out) == 2


def test_citation_noise_is_stripped_from_front_body() -> None:
    block = _block(
        "para",
        "Front. Plant Sci. 12:717552. while enviromic assembly enabled it by increasing "
        "yield plasticity doi: 10.3389/fpls.2021.717552 predictions. Furthermore, we discussed.",
        (66.2, 685.0, 550.5, 708.9),
        column_role="body",
    )

    out = _strip_front_citation_noise([block], SIDE)

    assert "Front." not in out[0].text
    assert "doi:" not in out[0].text
    assert out[0].text.startswith("while enviromic assembly")
    # 左界被信息栏拉宽时收回正文列起点
    assert out[0].bbox[0] >= 160.5