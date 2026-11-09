"""综述标准差距检查：骨架、证据与引用门槛。"""

from __future__ import annotations

from pathlib import Path

from server.research.review_standard import evaluate, resolve_template, template_sections

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
    assert label == "系统综述（PRISMA 风格）"
    assert "检索策略" in sections
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
    assert "临床证据" in sections

    # 选择不存在的文件要报错
    _, _, error = resolve_template("不存在.md", files)
    assert "没有找到模板" in error
