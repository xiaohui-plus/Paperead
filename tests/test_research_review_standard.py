"""综述标准差距检查：骨架、证据与引用门槛。"""

from __future__ import annotations

from pathlib import Path

from server.research.review_standard import (
    BUILTIN_TEMPLATES,
    DEFAULT_TEMPLATE,
    evaluate,
    resolve_template,
    template_sections,
)

DRAFT = """# 某主题综述

## 摘要
本文总结……

## 引言
背景……

## 方法
检索……

## 机制
机制……

## 临床证据
证据……

## 生物标志物
标志物……

## 讨论
争议……

## 结论
结论……
"""


def _audit(**overrides):
    metrics = {
        "fulltextSources": 6,
        "citationsTotal": 20,
        "pageRatio": 0.8,
        "unresolved": 0,
        "uncitedClaims": 0,
    }
    metrics.update(overrides)
    return {
        "metrics": metrics,
        "sections": [
            {"title": "摘要", "sources": 1},
            {"title": "引言", "sources": 1},
            {"title": "机制", "sources": 3},
            {"title": "临床证据", "sources": 4},
            {"title": "生物标志物", "sources": 2},
            {"title": "讨论", "sources": 2},
        ],
    }


def test_passing_draft_has_no_gaps():
    report = evaluate(DRAFT, _audit())

    assert report["passed"] is True
    assert report["score"] == 100
    assert report["gaps"] == []
    assert report["template"] == "default-5-section"


def test_missing_sections_and_weak_evidence_are_reported():
    draft = "# 综述\n\n## 一、单药失败\n\n正文。\n"
    report = evaluate(
        draft,
        _audit(fulltextSources=2, citationsTotal=4, pageRatio=0.2),
    )

    assert report["passed"] is False
    assert "摘要" in report["missingSections"]
    assert "方法/检索策略" in report["missingSections"]
    assert any("本地全文来源" in gap for gap in report["gaps"])
    assert any("页码级引用率" in gap for gap in report["gaps"])
    assert report["actions"]


def test_template_file_overrides_required_sections(tmp_path: Path):
    template = tmp_path / "review-template.md"
    template.write_text(
        "# 5A 模板\n\n## 要点\n\n## 方法\n\n## 证据\n\n## 讨论\n\n## 结论\n",
        encoding="utf-8",
    )

    assert template_sections(template) == ["要点", "方法", "证据", "讨论", "结论"]

    report = evaluate("# 草稿\n\n## 方法\n", _audit(), template)
    assert report["template"] == "review-template.md"
    assert report["missingSections"] == ["要点", "证据", "讨论", "结论"]


def test_template_priority_builtin_uploaded_default(tmp_path: Path):
    files = tmp_path / "files"
    files.mkdir()

    # 没有选择也没有上传 → 系统默认
    sections, label, error = resolve_template(None, files)
    assert sections is None
    assert label == "default-5-section"
    assert error == ""

    # 明确选内置模板
    sections, label, error = resolve_template("systematic", files)
    assert label == "系统综述（PRISMA 2020）"
    assert "方法" in sections and "结果" in sections
    assert error == ""

    # 用户上传的模板在未选择时生效
    (files / "我的5A模板.md").write_text(
        "## 背景\n\n## 方法\n\n## 结果\n", encoding="utf-8"
    )
    sections, label, _ = resolve_template(None, files)
    assert label == "我的5A模板.md"
    assert sections == ["背景", "方法", "结果"]

    # 显式选择优先于上传文件
    sections, label, _ = resolve_template("narrative", files)
    assert label == "叙述性综述"
    assert "机制与证据" in sections

    # 选择不存在的文件要报错
    _, _, error = resolve_template("不存在.md", files)
    assert "没有找到模板" in error


def test_builtin_templates_cover_paper_and_review_with_constraints():
    paper = BUILTIN_TEMPLATES["paper-empirical"]
    assert paper["kind"] == "paper"
    titles = [item["title"] for item in paper["outline"]]
    for expected in ["摘要", "引言", "方法", "实验与结果", "结论"]:
        assert expected in titles
    assert all(item["words"] and item["points"] and item["checks"] for item in paper["outline"])
    assert "必写" in paper["markdown"] and "自检" in paper["markdown"]

    for template_id in ("narrative", "systematic"):
        template = BUILTIN_TEMPLATES[template_id]
        assert template["kind"] == "review"
        assert all(item["points"] for item in template["outline"])
        assert "必写" in template["markdown"]

    assert DEFAULT_TEMPLATE["kind"] == "review"
    assert any("摘要" in item["title"] for item in DEFAULT_TEMPLATE["outline"])


def test_template_options_expose_kind_outline_and_markdown(tmp_path):
    from server.research import templates

    items = templates.list_options(tmp_path)
    by_id = {item["id"]: item for item in items}
    assert by_id["paper-empirical"]["kind"] == "paper"
    assert by_id["paper-empirical"]["outline"]
    assert by_id["systematic"]["kind"] == "review"
    assert by_id["default"]["outline"]

    detail = templates.describe("paper-empirical", tmp_path, None)
    assert detail["kind"] == "paper"
    assert detail["markdown"].startswith("# 实证研究论文")
    assert detail["outline"][0]["checks"]
    assert detail["sections"][:2] == ["标题与作者", "摘要"]
