from __future__ import annotations

import base64
from pathlib import Path

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.research.docx_edit import DocxError, edit_docx, read_docx
from server.research.files import save_bytes
from server.research.store import ResearchStore
from server.research.tools_api import build_router

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def make_docx(path: Path) -> None:
    document = Document()
    document.add_heading("综述草稿", level=1)
    document.add_paragraph("第一段：方法部分待补充。")
    document.add_paragraph("第二段：结果部分需要引用。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "指标"
    table.cell(0, 1).text = "数值"
    table.cell(1, 0).text = "准确率"
    table.cell(1, 1).text = "85%"
    document.save(path)


@pytest.fixture()
def store(tmp_path: Path) -> ResearchStore:
    return ResearchStore(tmp_path / "research")


@pytest.fixture()
def client(store: ResearchStore):
    app = FastAPI()
    app.include_router(
        build_router(store, None, lambda *args: {}, lambda row: row)
    )
    with TestClient(app) as test_client:
        yield test_client


def test_read_docx_structure(tmp_path: Path):
    path = tmp_path / "draft.docx"
    make_docx(path)
    data = read_docx(path)

    assert data["totalParagraphs"] == 3
    assert data["totalTables"] == 1
    assert data["outline"] == [{"index": 0, "level": 1, "text": "综述草稿"}]
    assert data["paragraphs"][1]["text"].startswith("第一段")
    assert data["tables"][0]["rows"][1] == ["准确率", "85%"]


def test_edit_applies_all_operations(tmp_path: Path):
    path = tmp_path / "draft.docx"
    make_docx(path)
    result = edit_docx(
        path,
        [
            {"op": "replace", "find": "待补充", "replace": "已补充"},
            {"op": "set_paragraph", "index": 2, "text": "第二段：结果已补引用。"},
            {"op": "insert_paragraph", "after": 2, "text": "新增：讨论局限。"},
            {"op": "set_table_cell", "table": 0, "row": 1, "column": 1, "text": "92%"},
            {"op": "delete_paragraph", "index": 1},
            {"op": "append_paragraph", "text": "结尾：下一步计划。"},
        ],
    )

    assert result["removedParagraphs"] == 1
    assert [item["op"] for item in result["applied"]] == [
        "replace",
        "set_paragraph",
        "insert_paragraph",
        "set_table_cell",
        "delete_paragraph",
        "append_paragraph",
    ]
    data = read_docx(path)
    texts = [row["text"] for row in data["paragraphs"]]
    assert "综述草稿" in texts
    assert "第二段：结果已补引用。" in texts
    assert "新增：讨论局限。" in texts
    assert "结尾：下一步计划。" in texts
    assert all("待补充" not in text for text in texts)
    assert data["tables"][0]["rows"][1][1] == "92%"


def test_edit_rejects_bad_operations(tmp_path: Path):
    path = tmp_path / "draft.docx"
    make_docx(path)

    with pytest.raises(DocxError):
        edit_docx(path, [])
    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "set_paragraph", "index": 99, "text": "越界"}])
    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "unknown"}])
    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "insert_paragraph", "after": 0, "text": "x", "style": "不存在的样式"}])


def test_docx_api_read_edit_and_size(store: ResearchStore, client: TestClient, tmp_path: Path):
    project = store.create_project("research", "Word 项目")
    session = store.create_session(project["id"], "会话")
    workspace = Path(project["workspace"])
    source = tmp_path / "draft.docx"
    make_docx(source)
    relative, size = save_bytes(workspace, "draft.docx", source.read_bytes())
    item = store.add_file(
        project["id"], "draft.docx", relative, mime=DOCX_MIME, size=size
    )

    base = f"/internal/research/sessions/{session['id']}/docx"
    read = client.get(base, params={"path": "draft.docx"})
    assert read.status_code == 200
    assert read.json()["file"]["path"] == "draft.docx"

    edited = client.post(
        base,
        json={
            "path": "draft.docx",
            "operations": [
                {"op": "replace", "find": "待补充", "replace": "已补充"},
                {"op": "append_paragraph", "text": "新的总结段。"},
                {"op": "format_paragraph", "index": 1, "fontSize": 11, "lineSpacing": 1.5},
            ],
        },
    )
    assert edited.status_code == 200
    body = edited.json()
    assert body["applied"][0]["occurrences"] == 1
    assert body["applied"][2]["fields"] == ["fontSize", "lineSpacing"]
    assert body["file"]["size"] > 0

    saved = Document(str(next(workspace.rglob("draft.docx"))))
    assert saved.paragraphs[1].runs[0].font.size.pt == 11
    assert saved.paragraphs[1].paragraph_format.line_spacing == 1.5

    again = client.get(base, params={"path": "draft.docx"}).json()
    assert "新的总结段。" in [row["text"] for row in again["paragraphs"]]

    refreshed = next(row for row in store.list_items(project["id"]) if row["id"] == item["id"])
    assert refreshed["size"] == body["file"]["size"]


def test_format_paragraph_applies_font_indent_and_spacing(tmp_path: Path):
    path = tmp_path / "format.docx"
    make_docx(path)
    result = edit_docx(
        path,
        [
            {
                "op": "format_paragraph",
                "index": 1,
                "font": "宋体",
                "fontSize": 12,
                "bold": True,
                "align": "justify",
                "firstLineIndentChars": 2,
                "lineSpacing": 1.5,
                "spaceBeforePt": 6,
                "spaceAfterPt": 6,
            }
        ],
    )

    assert result["applied"][0]["indices"] == [1]
    document = Document(str(path))
    paragraph = document.paragraphs[1]
    run = paragraph.runs[0]
    assert run.font.name == "宋体"
    assert run._element.rPr.rFonts.get(qn("w:eastAsia")) == "宋体"
    assert run.font.size.pt == 12
    assert run.font.bold is True
    assert paragraph.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY
    assert paragraph.paragraph_format.line_spacing == 1.5
    assert paragraph.paragraph_format.first_line_indent.pt == 24
    assert paragraph.paragraph_format.space_before.pt == 6
    assert paragraph.paragraph_format.space_after.pt == 6
    ind = paragraph._p.pPr.find(qn("w:ind"))
    assert ind is not None and ind.get(qn("w:firstLineChars")) == "200"


def test_format_paragraph_targets_indices_and_validates(tmp_path: Path):
    path = tmp_path / "format.docx"
    make_docx(path)
    edit_docx(
        path,
        [
            {
                "op": "format_paragraph",
                "indices": [0, 2],
                "fontSize": 14,
                "italic": True,
                "color": "1A2B3C",
            }
        ],
    )
    document = Document(str(path))
    assert document.paragraphs[0].runs[0].font.size.pt == 14
    assert document.paragraphs[0].runs[0].font.italic is True
    assert document.paragraphs[2].runs[0].font.size.pt == 14
    assert document.paragraphs[1].runs[0].font.size is None

    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "format_paragraph", "index": 0, "align": "middle"}])
    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "format_paragraph", "index": 0}])
    with pytest.raises(DocxError):
        edit_docx(
            path,
            [
                {
                    "op": "format_paragraph",
                    "index": 0,
                    "firstLineIndentChars": 2,
                    "firstLineIndentPt": 24,
                }
            ],
        )
    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "format_paragraph", "index": 99, "bold": True}])


def test_insert_and_edit_tables(tmp_path: Path):
    path = tmp_path / "tables.docx"
    make_docx(path)
    result = edit_docx(
        path,
        [
            {
                "op": "insert_table",
                "after": 2,
                "rows": 2,
                "columns": 2,
                "data": [["指标", "数值"], ["准确率", "85%"]],
                "header": True,
                "borders": "grid",
                "widthsPt": [120, 200],
                "align": "center",
            },
            {"op": "table_add_row", "table": 0, "data": ["召回率", "78%"]},
            {"op": "table_add_column", "table": 0, "data": ["备注", "样本 n=200"]},
            {"op": "table_add_row", "table": 0, "before": 1, "data": ["新增", "1", "2"]},
            {"op": "table_delete_row", "table": 0, "row": 3},
            {"op": "table_merge_cells", "table": 0, "startRow": 0, "startColumn": 0, "endRow": 0, "endColumn": 1},
            {"op": "table_set", "table": 0, "borders": "outer", "widthsPt": [110, 180, 220]},
        ],
    )
    assert [item["op"] for item in result["applied"]][:2] == ["insert_table", "table_add_row"]

    document = Document(str(path))
    assert len(document.tables) == 2
    table = document.tables[0]
    assert len(table.rows) == 3
    assert len(table.columns) == 3
    assert "指标" in table.rows[0].cells[0].text
    assert "数值" in table.rows[0].cells[0].text
    assert table.rows[2].cells[2].text == "样本 n=200"
    assert table.rows[0]._tr.trPr.find(qn("w:tblHeader")) is not None
    assert document.tables[1].rows[1].cells[1].text == "85%"


def test_table_boundaries_and_delete(tmp_path: Path):
    path = tmp_path / "table-errors.docx"
    make_docx(path)
    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "table_delete_row", "table": 0, "row": 99}])
    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "table_set", "table": 1, "borders": "none"}])
    edit_docx(path, [{"op": "delete_table", "table": 0}])
    assert Document(str(path)).tables == []


def test_style_heading_and_outline(tmp_path: Path):
    path = tmp_path / "styles.docx"
    make_docx(path)
    edit_docx(
        path,
        [
            {
                "op": "create_style",
                "name": "论文正文",
                "base": "Normal",
                "font": "宋体",
                "fontSize": 12,
                "align": "justify",
                "firstLineIndentChars": 2,
                "lineSpacing": 1.5,
            },
            {"op": "set_style", "indices": [1, 2], "style": "论文正文"},
            {"op": "set_heading", "index": 0, "level": 2},
            {"op": "modify_style", "style": "论文正文", "fontSize": 13},
        ],
    )
    document = Document(str(path))
    assert document.paragraphs[1].style.name == "论文正文"
    assert document.paragraphs[2].style.name == "论文正文"
    style = next(item for item in document.styles if item.name == "论文正文")
    assert style.font.name == "宋体"
    assert style.font.size.pt == 13
    assert style.paragraph_format.line_spacing == 1.5
    assert style.element.rPr.rFonts.get(qn("w:eastAsia")) == "宋体"
    assert "Heading" in document.paragraphs[0].style.name or "标题" in document.paragraphs[0].style.name
    outline = document.paragraphs[0]._p.pPr.find(qn("w:outlineLvl"))
    assert outline is not None and outline.get(qn("w:val")) == "1"

    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "create_style", "name": "论文正文"}])
    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "set_style", "index": 1, "style": "不存在的样式"}])


def test_page_header_footer_breaks(tmp_path: Path):
    path = tmp_path / "page.docx"
    make_docx(path)
    edit_docx(
        path,
        [
            {
                "op": "set_page",
                "size": "A4",
                "orientation": "landscape",
                "marginTopCm": 2.0,
                "marginBottomCm": 2.0,
                "marginLeftCm": 2.5,
                "marginRightCm": 2.5,
            },
            {"op": "set_header", "text": "综述草稿", "pageNumber": True, "align": "right"},
            {"op": "set_footer", "pageNumber": True, "align": "center"},
            {"op": "insert_page_break", "after": 1},
            {"op": "insert_section_break", "after": 2, "type": "nextPage"},
        ],
    )
    document = Document(str(path))
    section = document.sections[0]
    assert round(section.page_width.cm, 1) == 29.7
    assert round(section.page_height.cm, 1) == 21.0
    assert round(section.top_margin.cm, 1) == 2.0
    assert "综述草稿" in section.header.paragraphs[0].text
    assert section.header.paragraphs[0]._p.find(qn("w:fldSimple")) is not None
    assert section.footer.paragraphs[0]._p.find(qn("w:fldSimple")) is not None
    assert len(document.sections) == 2
    breaks = document.element.body.findall(".//" + qn("w:br"))
    assert any(item.get(qn("w:type")) == "page" for item in breaks)


def test_list_image_hyperlink_and_toc(tmp_path: Path):
    path = tmp_path / "media.docx"
    make_docx(path)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lK3jZQAAAABJRU5ErkJggg=="
    )
    (tmp_path / "figure.png").write_bytes(png)
    edit_docx(
        path,
        [
            {"op": "insert_list", "after": 2, "items": ["第一点", "第二点"], "ordered": True},
            {
                "op": "insert_image",
                "after": 1,
                "image": "figure.png",
                "widthCm": 2,
                "align": "center",
                "caption": "图 1 示意图",
            },
            {"op": "insert_hyperlink", "after": 0, "text": "参考链接", "url": "https://example.com"},
            {"op": "insert_toc", "levels": "1-2"},
        ],
        workspace=tmp_path,
    )
    document = Document(str(path))
    texts = [paragraph.text for paragraph in document.paragraphs]
    assert "第一点" in texts and "第二点" in texts
    assert "图 1 示意图" in texts
    assert len(document.inline_shapes) == 1
    hyperlinks = document.element.body.findall(".//" + qn("w:hyperlink"))
    assert len(hyperlinks) == 1
    assert document.paragraphs[0]._p.find(qn("w:fldSimple")) is not None
    assert "TOC" in document.paragraphs[0]._p.find(qn("w:fldSimple")).get(qn("w:instr"))

    with pytest.raises(DocxError):
        edit_docx(path, [{"op": "insert_image", "image": "../outside.png"}], workspace=tmp_path)


def test_docx_api_rejects_non_docx(store: ResearchStore, client: TestClient):
    project = store.create_project("research", "Word 项目")
    session = store.create_session(project["id"], "会话")
    workspace = Path(project["workspace"])
    relative, size = save_bytes(workspace, "notes.md", b"# draft")
    store.add_file(project["id"], "notes.md", relative, mime="text/markdown", size=size)

    response = client.get(
        f"/internal/research/sessions/{session['id']}/docx",
        params={"path": "notes.md"},
    )
    assert response.status_code == 400
