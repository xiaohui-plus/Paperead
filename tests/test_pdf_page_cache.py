"""Phase 2：页级译文 PDF 缓存与增量重建。"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from paperead.pdf_export import build_translated_pdf, render_page


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    for index in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {index + 1} title", fontsize=16)
        page.insert_text(
            (72, 110),
            f"Page {index + 1} has enough English body text for layout.",
            fontsize=10,
        )
    doc.save(path)
    doc.close()


def _blocks() -> list[dict]:
    return [
        {
            "id": "b1",
            "kind": "para",
            "page": 1,
            "bbox": [72, 100, 520, 150],
            "text": "Page 1 has enough English body text for layout.",
            "translated": "第一页有足够的英文正文用于排版。",
            "translatable": True,
        },
        {
            "id": "b2",
            "kind": "para",
            "page": 2,
            "bbox": [72, 100, 520, 150],
            "text": "Page 2 has enough English body text for layout.",
            "translated": "第二页有足够的英文正文用于排版。",
            "translatable": True,
        },
    ]


def test_page_cache_reuses_unchanged_pages(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    _make_pdf(pdf)
    out = tmp_path / "out"

    first = build_translated_pdf(pdf, _blocks(), out, lang="zh-Hans", cache_key="v1")
    assert first["pages"] == 2
    assert first["pages_rendered"] == 2
    # 默认从 11.5 起自动收敛，不能继续固定在 8.6。
    assert first["body_size_used"] > 8.6
    assert first["pages_reused"] == 0
    assert (out / "translated.pdf").is_file()
    assert (out / "pages.meta.json").is_file()

    blocks = _blocks()
    blocks[1]["translated"] = "第二页的译文改了，只需要重排这一页。"
    second = build_translated_pdf(pdf, blocks, out, lang="zh-Hans", cache_key="v2")
    assert second["pages"] == 2
    assert second["pages_rendered"] == 1
    assert second["pages_reused"] == 1
    assert second["page_size_ok"] is True

    # 整篇 blocks 未变：全局缓存直接命中，不再打开页面渲染。
    third = build_translated_pdf(pdf, blocks, out, lang="zh-Hans", cache_key="v2")
    assert third["cached"] is True
    assert third["pages_rendered"] == 0
    assert third["pages_reused"] == 2

def test_body_block_fills_spare_height_with_leading(tmp_path: Path) -> None:
    """字号统一后，块内剩余高度应靠行距填充，而不是留一大片空白。"""
    pdf = tmp_path / "gap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Source paragraph.", fontsize=10)
    doc.save(pdf)
    doc.close()

    blocks = [
        {
            "id": "b1",
            "kind": "para",
            "page": 1,
            "bbox": [72, 100, 520, 320],
            "text": "Source paragraph.",
            "translated": "这是一段用于验证行距填充的较长中文译文，它应当至少占据两行以上的空间，以便把原始英文段落留下的高度补齐。",
            "translatable": True,
        }
    ]
    res = render_page(
        pdf,
        blocks,
        0,
        None,
        dry_run=True,
        body_size=10.75,
        line_height=1.15,
    )
    layout = res.layouts[0]
    assert layout.line_height > 1.15
    assert layout.line_height <= 1.4
    assert layout.needed <= layout.available
