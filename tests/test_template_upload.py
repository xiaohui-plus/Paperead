import io
from pathlib import Path

import pytest
from docx import Document

from server.research import templates


def test_docx_upload_extracts_heading_structure(tmp_path):
    document = Document()
    document.add_heading("摘要", level=1)
    document.add_heading("研究背景", level=1)
    document.add_heading("研究方法", level=1)
    document.add_heading("结果与讨论", level=2)
    document.add_paragraph("这里是正文，不应该被当成章节。")

    buffer = io.BytesIO()
    document.save(buffer)
    root = tmp_path / "review-templates"

    item = templates.save_upload(root, "参考论文.docx", buffer.getvalue())
    assert item["id"] == "upload:参考论文.md"
    assert item["sections"] == ["摘要", "研究背景", "研究方法", "结果与讨论"]

    assert (root / "uploads" / "参考论文.md").is_file()
    assert (root / "raw" / "参考论文.docx").is_file()
    assert templates.read_choice(root) == "upload:参考论文.md"

    detail = templates.describe("upload:参考论文.md", root, None)
    assert detail["name"] == "参考论文"
    assert detail["sections"] == item["sections"]
    assert detail["markdown"].startswith("# 参考论文")


def test_docx_upload_rejects_document_without_headings(tmp_path):
    document = Document()
    document.add_paragraph("只有正文，没有任何标题样式。")
    buffer = io.BytesIO()
    document.save(buffer)

    with pytest.raises(templates.TemplateError):
        templates.save_upload(tmp_path / "review-templates", "无标题.docx", buffer.getvalue())


def test_pdf_upload_extracts_sections_and_guide(tmp_path):
    candidates = sorted(Path("data/papers").glob("*.pdf"))
    pdf = next(
        (
            path
            for path in candidates
            if len(templates._upload_pdf_sections(path)) >= 2
        ),
        None,
    )
    if pdf is None:
        pytest.skip("没有可用于 PDF 模板解析的测试论文")

    root = tmp_path / "review-templates"
    item = templates.save_upload(root, "参考综述.pdf", pdf.read_bytes())

    assert item["id"] == "upload:参考综述.md"
    assert len(item["sections"]) >= 2
    lowered = " ".join(item["sections"]).lower()
    assert not any(
        word in lowered
        for word in ("data availability", "author contributions", "competing interests")
    )
    assert (root / "uploads" / "参考综述.md").is_file()
    assert (root / "uploads" / "参考综述.guide.json").is_file()
    assert (root / "raw" / "参考综述.pdf").is_file()

    detail = templates.describe("upload:参考综述.md", root, None)
    assert detail["sections"] == item["sections"]
    assert detail["guide"], "PDF 模板应带图表/格式/语言指南"
    assert {"figures", "tables", "format", "style"} & set(detail["guide"])

    assert templates.delete_upload(root, "参考综述.md") is True
    assert not (root / "uploads" / "参考综述.md").exists()
    assert not (root / "uploads" / "参考综述.guide.json").exists()
    assert not (root / "raw" / "参考综述.pdf").exists()
    assert templates.read_choice(root) == "default"