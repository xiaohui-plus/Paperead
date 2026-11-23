"""回填层：公式 token 不丢失、回抄判定不吞正文。"""

from __future__ import annotations

import pymupdf

from paperead.pdf_export import (
    _inline_atoms,
    _looks_like_formula_source,
    is_formula_echo,
)


def _font() -> pymupdf.Font:
    return pymupdf.Font(fontname="helv")


def test_formula_token_survives_missing_pixmap() -> None:
    tokens = [
        {"type": "text", "text": "其中 "},
        {"type": "math", "meta": {"bbox": [10, 20, 40, 32], "latex": "q_i", "source": "qi"}},
        {"type": "text", "text": " 表示查询"},
    ]
    atoms = _inline_atoms(_font(), tokens, 9.5, {})
    math_atoms = [a for a in atoms if a["type"] == "math"]
    assert len(math_atoms) == 1
    atom = math_atoms[0]
    assert atom["pixmap"] is None
    assert atom["latex"] == "q_i"
    assert atom["source"] == "qi"
    assert atom["w"] > 0 and atom["h"] > 0


def test_formula_token_keeps_source_without_latex_or_bbox() -> None:
    tokens = [{"type": "math", "meta": {"source": "ψ", "latex": ""}}]
    atoms = _inline_atoms(_font(), tokens, 9.5, {})
    assert [a["type"] for a in atoms] == ["math"]
    assert atoms[0]["source"] == "ψ"
    assert atoms[0]["w"] > 0


def test_prose_source_is_not_formula() -> None:
    assert not _looks_like_formula_source(
        "However, such an approach may miss some important features in the graph."
    )


def test_formula_source_is_recognized() -> None:
    assert _looks_like_formula_source("Attn(h_i, h_j) = softmax(a_{ij}) · v_j")
    assert _looks_like_formula_source("q_i = h_i W^Q")


def test_english_translation_of_prose_is_not_formula_echo() -> None:
    block = {
        "kind": "para",
        "text": (
            "However, such an approach may miss some important features in the graph, "
            "which leads to worse predictions on large molecules."
        ),
        "translated": "However, such an approach may miss some important features.",
    }
    assert not is_formula_echo(block)


def test_equation_block_always_kept_as_formula() -> None:
    block = {
        "kind": "equation",
        "text": "a = b",
        "translated": "a = b",
    }
    assert is_formula_echo(block)


def test_formula_line_echo_is_detected() -> None:
    block = {
        "kind": "para",
        "text": "Attn(h_i, h_j) = softmax(a_{ij}) · v_j",
        "translated": "Attn(h_i, h_j) = softmax(a_{ij}) · v_j",
    }
    assert is_formula_echo(block)