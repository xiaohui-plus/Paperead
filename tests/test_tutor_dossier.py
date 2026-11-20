"""论文档案与历史曲线：一次构建、整篇覆盖、超预算降级。"""

from __future__ import annotations

from paperead.tutor import dossier as dossier_engine
from paperead.tutor import qa as qa_engine


def _block(block_id, order, page, kind, text, **extra):
    data = {
        "id": block_id,
        "order": order,
        "page": page,
        "kind": kind,
        "text": text,
        "bbox": [0, 0, 100, 10],
    }
    data.update(extra)
    return data


BLOCKS = [
    _block("b0", 0, 1, "title", "Pairwise Structure Encoding"),
    _block("b1", 1, 1, "abstract", "We study pairwise structural features for graphs."),
    _block("b2", 2, 1, "keywords", "graph neural network; structural encoding"),
    _block("b3", 3, 1, "heading", "1 Introduction"),
    _block(
        "b4",
        4,
        1,
        "para",
        "Graph neural networks aggregate neighborhood information. "
        "Pairwise structure encoding keeps the shortest path distance between nodes. "
        "This lets the model reason about long range relations.",
        translated="图神经网络聚合邻域信息。成对结构编码保留节点之间的最短路径距离。",
    ),
    _block("b5", 5, 1, "heading", "2 Method"),
    _block(
        "b6",
        6,
        2,
        "para",
        "The binary structure transformer encodes pairwise features with attention. "
        "We pool the encoded features before prediction.",
        translated="二元结构 Transformer 用注意力编码成对特征，预测前池化。",
    ),
    _block("b7", 7, 2, "figure", "", caption="Overview of the framework"),
    _block("b8", 8, 2, "equation", "", latex="d(i,j)=\\min_p |p|", eq_number="3"),
    _block("b9", 9, 3, "reference", "[1] Smith et al. Graph kernels. 2020."),
    _block("b10", 10, 3, "para", "Ablation shows pairwise encoding helps on long paths."),
]


def _dossier():
    return dossier_engine.build_dossier(
        "p1", {"title": "Pairwise Structure Encoding", "pages": 3}, BLOCKS
    )


def test_build_dossier_collects_outline_and_lists():
    dossier = _dossier()
    assert dossier.title == "Pairwise Structure Encoding"
    assert dossier.outline == [("1 Introduction", 1), ("2 Method", 1)]
    assert "pairwise structural features" in dossier.abstract
    assert "graph neural network" in dossier.keywords
    assert dossier.figures and "Overview of the framework" in dossier.figures[0]
    assert dossier.equations and "min_p" in dossier.equations[0]
    assert dossier.references and "Graph kernels" in dossier.references[0]
    # 只有正文段落进入 body（不含标题/图注/公式行）
    assert [c.block_id for c in dossier.body] == ["b4", "b6", "b10"]


def test_render_full_text_when_within_budget():
    dossier = _dossier()
    text, hits = dossier.render("pairwise structure encoding", budget=20_000)
    assert "【全篇正文（全文）】" in text
    assert "节选模式" not in text
    for chunk in dossier.body:
        assert chunk.source in text
    assert "【章节大纲】" in text and "1 Introduction" in text
    assert hits and hits[0].block_id in {"b4", "b6", "b10"}


def test_render_excerpt_keeps_every_paragraph_and_expands_top_hits():
    long_blocks = [
        _block(
            f"p{i}",
            i,
            1 + i // 2,
            "para",
            f"Paragraph {i}. " + ("pairwise structure distance method detail. " * 120),
        )
        for i in range(6)
    ]
    dossier = dossier_engine.build_dossier("p2", {"title": "Long"}, long_blocks)
    text, hits = dossier.render("pairwise structure distance", budget=4_000)
    assert "【全篇正文（节选模式）】" in text
    # 全篇覆盖：每段至少出现标签
    for chunk in dossier.body:
        assert f"[P{chunk.page}" in text
    # 命中段落给全文，且带「全文」标记
    assert "· 全文]" in text
    assert hits


def test_focus_matches_english_question_against_source_and_chinese_against_translation():
    dossier = _dossier()
    english = dossier.focus("binary structure transformer")
    chinese = dossier.focus("最短路径距离")
    assert english and english[0].block_id == "b6"
    assert chinese and chinese[0].block_id in {"b4", "b6"}


def test_format_quotes_marks_source_and_page():
    text = dossier_engine.format_quotes(
        [
            {"text": "It uses shortest path distance", "page": 1, "source": "en"},
            {"text": "最短路径距离", "page": 2, "source": "zh"},
            {"text": "", "page": 2},
        ]
    )
    assert "【用户引用】" in text
    assert "用户选中的原文片段 · 第 1 页" in text
    assert "用户选中的译文片段 · 第 2 页" in text
    assert text.count("### ") == 2


def test_history_curve_keeps_recent_turns_longer():
    turns = []
    for i in range(4):
        turns.append({"role": "user", "text": f"问题{i}" + "细节" * 200})
        turns.append({"role": "ai", "text": f"回答{i}" + "说明" * 200})
    text = qa_engine.history_curve(turns)
    assert text.startswith("【历史对话·越近越完整】")
    recent_line = next(line for line in text.splitlines() if "问题3" in line)
    early_line = next(line for line in text.splitlines() if "问题0" in line)
    # 最近一轮保留的问题细节应明显多于最早一轮
    assert len(recent_line) > len(early_line)
    assert "较早，细节可能已模糊" in text


def test_history_curve_empty():
    assert qa_engine.history_curve(None) == ""
    assert qa_engine.history_curve([]) == ""


def test_looks_like_followup_only_for_context_dependent_questions():
    assert qa_engine.looks_like_followup("那它和注意力有什么区别")
    assert qa_engine.looks_like_followup("这个方法为什么有效")
    assert not qa_engine.looks_like_followup("什么是图神经网络")
    assert not qa_engine.looks_like_followup("第二个问题")