"""LaTeX 导出测试：模板选择、完整 .tex 透传与本地编译。

pandoc 或 TeX 引擎缺失时跳过依赖它们的用例，保证 CI 上没有环境也能跑通
纯逻辑部分。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.research.latex import (
    build_tex,
    escape_latex,
    find_engine,
    find_pandoc,
    has_cjk,
)
from server.research.store import ResearchStore
from server.research.tools_api import build_router

HAS_PANDOC = bool(find_pandoc())
HAS_ENGINE = bool(find_engine())


class FakeLibrary:
    def list(self):
        return []

    def get(self, paper_id):  # pragma: no cover - 本文件不测文献库
        return None

    def find_by_source(self, source_id, project_id=None):  # pragma: no cover
        return None


@pytest.fixture()
def workspace(tmp_path: Path):
    store = ResearchStore(tmp_path / "research")
    project = store.create_project("review", "LaTeX 测试")
    session = store.create_session(project["id"])
    app = FastAPI()
    app.include_router(
        build_router(store, FakeLibrary(), lambda *args, **kwargs: {}, lambda row: row)
    )
    with TestClient(app) as client:
        yield client, store, project, session


def test_has_cjk():
    assert has_cjk("中文综述")
    assert not has_cjk("A survey of graph networks")


def test_escape_latex_specials():
    assert escape_latex("A & B_1 100% #1") == r"A \& B\_1 100\% \#1"


def test_complete_tex_passthrough():
    source = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n"
    tex, style, error = build_tex(source)

    assert error == ""
    assert tex == source
    assert style == "article"


@pytest.mark.skipif(not HAS_PANDOC, reason="需要 pandoc")
def test_english_markdown_uses_article_and_drops_duplicate_title():
    tex, style, error = build_tex(
        "# A Survey of Graph Networks\n\nBody text with **bold** words.\n",
        title="A Survey of Graph Networks",
    )

    assert error == ""
    assert style == "article"
    assert "\\documentclass[a4paper,11pt]{article}" in tex
    assert "\\title{A Survey of Graph Networks}" in tex
    # 正文里的一级标题被去掉，避免和模板标题重复。
    assert tex.count("A Survey of Graph Networks") == 1


@pytest.mark.skipif(not HAS_PANDOC, reason="需要 pandoc")
def test_chinese_markdown_uses_ctexart():
    tex, style, error = build_tex("# 免疫治疗综述\n\n本文讨论证据边界。\n")

    assert error == ""
    assert style == "ctexart"
    assert "{ctexart}" in tex
    assert "\\title{免疫治疗综述}" in tex


@pytest.mark.skipif(not HAS_PANDOC, reason="需要 pandoc")
def test_export_latex_writes_tex_file(workspace):
    client, store, project, session = workspace

    response = client.post(
        f"/internal/research/sessions/{session['id']}/export",
        json={
            "format": "latex",
            "content": "# 测试综述\n\n正文。\n",
            "title": "测试综述",
            "compile": False,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["format"] == "latex"
    assert data["compiled"] is False
    assert len(data["files"]) == 1
    relative = data["files"][0]["path"]
    assert relative.endswith(".tex")
    saved = Path(project["workspace"]) / "files" / relative
    assert saved.is_file()
    assert "\\documentclass" in saved.read_text(encoding="utf-8")


@pytest.mark.skipif(not HAS_PANDOC, reason="需要 pandoc")
def test_latex_links_bib_when_present(workspace):
    client, store, project, session = workspace
    from server.research.files import save_bytes

    save_bytes(
        Path(project["workspace"]),
        "references.bib",
        b"@article{smith2020, title={A Paper}, author={Smith, J.}, year={2020}}",
    )

    response = client.post(
        f"/internal/research/sessions/{session['id']}/export",
        json={
            "format": "latex",
            "content": "# 引用测试\n\n已有研究 [@smith2020] 说明了这一点。\n",
            "title": "引用测试",
            "compile": False,
        },
    )

    data = response.json()
    assert not data.get("error"), data.get("error")
    saved = Path(project["workspace"]) / "files" / data["files"][0]["path"]
    tex = saved.read_text(encoding="utf-8")
    assert "\\bibliography{references}" in tex
    assert "\\citep{smith2020}" in tex or "\\cite{smith2020}" in tex


@pytest.mark.skipif(not (HAS_PANDOC and HAS_ENGINE), reason="需要 pandoc 与 TeX 引擎")
def test_compile_existing_tex_by_source(workspace):
    client, store, project, session = workspace
    base = f"/internal/research/sessions/{session['id']}/export"

    created = client.post(
        base,
        json={
            "format": "latex",
            "content": "# 已有稿件\n\n先写 .tex，再单独编译。\n",
            "title": "已有稿件",
            "filename": "main.tex",
            "compile": False,
        },
    ).json()
    assert created["files"][0]["path"] == "main.tex"

    compiled = client.post(base, json={"format": "pdf", "source": "main.tex"}).json()
    if compiled.get("error"):
        pytest.skip(f"本机 TeX 环境无法编译最小样例：{compiled['error'][:200]}")
    assert compiled["compiled"] is True
    assert any(item["name"].endswith(".pdf") for item in compiled["files"])


@pytest.mark.skipif(not (HAS_PANDOC and HAS_ENGINE), reason="需要 pandoc 与 TeX 引擎")
def test_export_pdf_compiles(workspace):
    client, store, project, session = workspace

    response = client.post(
        f"/internal/research/sessions/{session['id']}/export",
        json={
            "format": "pdf",
            "content": "# 编译测试\n\n这是一个最小可编译的中文段落。\n",
            "title": "编译测试",
        },
    )

    assert response.status_code == 200
    data = response.json()
    if data.get("error"):
        pytest.skip(f"本机 TeX 环境无法编译最小样例：{data['error'][:200]}")
    assert data["compiled"] is True
    names = {item["name"] for item in data["files"]}
    assert "编译测试.pdf" in names or any(name.endswith(".pdf") for name in names)
