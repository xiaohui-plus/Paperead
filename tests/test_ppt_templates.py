import io
import json

import pytest
from pptx import Presentation

from server.research import deck, ppt_templates


def _deck_bytes(path) -> bytes:
    return path.read_bytes()


def test_builtin_templates_are_packaged():
    assert len(ppt_templates.BUILTIN_TEMPLATES) >= 3
    for item in ppt_templates.BUILTIN_TEMPLATES:
        path = ppt_templates.ASSETS_DIR / item["file"]
        assert path.is_file(), item["file"]
        assert "{{bullets}}" in "".join(
            shape.text_frame.text
            for slide in Presentation(str(path)).slides
            for shape in slide.shapes
            if getattr(shape, "has_text_frame", False)
        )


def test_choice_defaults_and_keeps_markdown_selection(tmp_path):
    root = tmp_path / "review-templates"
    assert ppt_templates.read_choice(root) == ppt_templates.DEFAULT_ID

    (root).mkdir(parents=True)
    (root / "settings.json").write_text(
        json.dumps({"selected": "upload:论文.md"}), encoding="utf-8"
    )
    ppt_templates.write_choice(root, "builtin:seminar")

    payload = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert payload["selected"] == "upload:论文.md"
    assert payload["ppt_selected"] == "builtin:seminar"
    assert ppt_templates.read_choice(root) == "builtin:seminar"


def test_resolve_builtin_and_unknown(tmp_path):
    root = tmp_path / "review-templates"
    data, label = ppt_templates.resolve(root, "builtin:academic")
    assert data and label == "内置：学术汇报"

    data, label = ppt_templates.resolve(root, "builtin:missing")
    assert data is None
    assert label == "builtin"


def test_upload_rejects_non_pptx_and_broken_file(tmp_path):
    root = tmp_path / "review-templates"
    with pytest.raises(ppt_templates.PptTemplateError):
        ppt_templates.save_upload(root, "template.pdf", b"%PDF")
    with pytest.raises(ppt_templates.PptTemplateError):
        ppt_templates.save_upload(root, "broken.pptx", b"not-a-pptx")


def test_upload_select_and_delete(tmp_path):
    root = tmp_path / "review-templates"
    source = ppt_templates.ASSETS_DIR / "academic.pptx"
    item = ppt_templates.save_upload(root, "组会模板.pptx", source.read_bytes())

    assert item["id"] == "upload:组会模板.pptx"
    assert ppt_templates.read_choice(root) == item["id"]
    data, label = ppt_templates.resolve(root)
    assert data
    assert label == "上传：组会模板"

    assert ppt_templates.delete_upload(root, "组会模板.pptx") is True
    assert ppt_templates.read_choice(root) == ppt_templates.DEFAULT_ID
    assert ppt_templates.list_uploads(root) == []


def test_builtin_template_can_render_slides(tmp_path):
    root = tmp_path / "review-templates"
    template, label = ppt_templates.resolve(root, "builtin:academic")
    assert label == "内置：学术汇报"

    slides = [
        {"kind": "title", "title": "肝癌免疫治疗耐药", "subtitle": "组会汇报"},
        {"kind": "section", "title": "研究背景"},
        {
            "kind": "bullets",
            "title": "耐药主要来自三条通路",
            "bullets": ["IFN-γ 通路失活", "T 细胞耗竭"],
            "chart": "柱状图：两组 ORR 对比",
            "source": "Li 2026, p3",
            "notes": "先讲结论。",
        },
    ]
    blob = deck.build_deck(slides, title="肝癌免疫治疗耐药", template=template)
    result = Presentation(io.BytesIO(blob))
    assert len(result.slides) == 3

    text = "\n".join(
        shape.text_frame.text
        for slide in result.slides
        for shape in slide.shapes
        if getattr(shape, "has_text_frame", False)
    )
    assert "{{" not in text
    assert "肝癌免疫治疗耐药" in text
    assert "IFN-γ 通路失活" in text
    assert "图表：柱状图：两组 ORR 对比" in text

def test_export_pptx_uses_selected_template_by_default(tmp_path):
    from server.research import tools_api

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    research_root = tmp_path / "research"
    research_root.mkdir()
    root = ppt_templates.root_for(research_root)
    ppt_templates.write_choice(root, "builtin:seminar")

    class FakeResearch:
        root = str(research_root)

        def workspace_root(self, _pid):
            return str(workspace)

        def add_item(self, *_args, **_kwargs):
            return {"id": "item-1"}

    body = tools_api.ExportIn(
        format="pptx",
        title="组会汇报",
        filename="组会汇报.pptx",
        deck=[
            {"kind": "title", "title": "组会汇报", "subtitle": "实验室例会"},
            {
                "kind": "bullets",
                "title": "结论",
                "bullets": ["结论一", "结论二"],
                "source": "项目文件",
            },
        ],
    )
    result = tools_api._export_pptx(FakeResearch(), {"id": "p_test"}, body)

    assert result["template"] == "内置：组会汇报"
    files = result["files"]
    assert len(files) == 1
    output = workspace / files[0]["name"]
    assert output.is_file()

    text = "\n".join(
        shape.text_frame.text
        for slide in Presentation(str(output)).slides
        for shape in slide.shapes
        if getattr(shape, "has_text_frame", False)
    )
    assert "{{" not in text
    assert "结论一" in text