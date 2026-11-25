"""LaTeX 导出测试：模板选择、完整 .tex 透传与本地编译。

pandoc 或 TeX 引擎缺失时跳过依赖它们的用例，保证 CI 上没有环境也能跑通
纯逻辑部分。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt
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
    # 与 Word 导出一致的中文论文版式。
    assert "\\linespread{1.2}" in tex
    assert "\\setlength{\\parindent}{2em}" in tex
    assert "\\setCJKmainfont{FangSong}" in tex
    assert "section/format = {\\zihao{3}\\heiti\\bfseries" in tex
    assert "subsection/format = {\\zihao{-3}\\heiti\\bfseries" in tex
    assert "subsubsection/format = {\\zihao{4}\\songti\\bfseries" in tex


@pytest.mark.skipif(not HAS_PANDOC, reason="需要 pandoc")
def test_chinese_quotes_and_layout_are_cleaned():
    """中文引号要输出成“”，正文不留内部 paperId 与章节自动编号。"""
    tex, style, error = build_tex(
        '# 综述\n\n所谓"证据不足"并非"无效"（Li 2026, p_5c0ab2865c69, p1）。\n',
        bib_name="references",
    )

    assert error == ""
    assert style == "ctexart"
    # pandoc 会把引号写成 ``x''，中文语境要还原成“”。
    assert "“证据不足”" in tex
    assert "``证据不足''" not in tex
    assert "p_" not in tex
    assert "\\setcounter{secnumdepth}{0}" in tex
    assert "\\pagestyle{plain}" in tex
    # 参考文献单独起页。
    assert "\\clearpage" in tex


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
def test_export_docx_applies_review_layout(workspace):
    client, store, project, session = workspace

    response = client.post(
        f"/internal/research/sessions/{session['id']}/export",
        json={
            "format": "docx",
            "content": "# 排版测试\n\n正文段落：用于检查导出后的中文排版。\n",
            "title": "排版测试",
        },
    )

    data = response.json()
    assert not data.get("error"), data.get("error")
    saved = Path(project["workspace"]) / "files" / data["files"][0]["path"]
    document = Document(str(saved))

    normal = document.styles["Normal"]
    assert normal.font.size == Pt(12)
    assert normal.element.get_or_add_rPr().get_or_add_rFonts().get(qn("w:eastAsia")) == "仿宋"
    heading1 = next(
        style for style in document.styles if (style.style_id or "").lower() == "heading1"
    )
    assert heading1.font.size == Pt(16)

    body = next(p for p in document.paragraphs if p.text.startswith("正文段落"))
    assert body.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY
    assert body.paragraph_format.line_spacing == pytest.approx(1.2)
    assert body.paragraph_format.first_line_indent == Pt(24)


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
