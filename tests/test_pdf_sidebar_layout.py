"""P0：页缘信息侧栏独立排版（L1 区域识别 + 独立字号 + 侧栏翻译回填）。"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf

from paperead.pdf_export import (
    SIDEBAR_MAX_SIZE,
    SIDEBAR_MIN_SIZE,
    build_translated_pdf,
    render_page,
)

SIDEBAR_X1 = 112.0
BODY_X0 = 344.0


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    for index, line in enumerate(
        ("Department of Oncology,", "City Hospital,", "Boston, MA, USA")
    ):
        page.insert_text((28, 106 + index * 16), line, fontsize=9)
    page.insert_text((BODY_X0, 106), "Body paragraph about the trial.", fontsize=11)
    page.insert_text((BODY_X0, 126), "It continues with more English text.", fontsize=11)
    page.insert_text((BODY_X0, 146), "And a third line for layout probing.", fontsize=11)
    doc.save(path)
    doc.close()


def _blocks() -> list[dict]:
    side = [
        ("s1", "Department of Oncology,", "肿瘤科，"),
        ("s2", "City Hospital,", "城市医院，"),
        ("s3", "Boston, MA, USA", "美国马萨诸塞州波士顿"),
    ]
    blocks = [
        {
            "id": block_id,
            "kind": "affiliation",
            "page": 1,
            "bbox": [28, 96 + index * 16, SIDEBAR_X1, 108 + index * 16],
            "text": text,
            "translated": translated,
            "translatable": True,
        }
        for index, (block_id, text, translated) in enumerate(side)
    ]
    blocks.append(
        {
            "id": "b1",
            "kind": "para",
            "page": 1,
            "bbox": [BODY_X0, 96, 558, 170],
            "text": (
                "Body paragraph about the trial. It continues with more English "
                "text. And a third line for layout probing."
            ),
            "translated": (
                "关于这项试验的正文段落。它继续包含更多英文内容。"
                "第三行用于验证排版。"
            ),
            "translatable": True,
        }
    )
    return blocks


def test_sidebar_detected_and_translated(tmp_path: Path) -> None:
    pdf = tmp_path / "side.pdf"
    _make_pdf(pdf)
    out_pdf = tmp_path / "p1.pdf"

    res = render_page(
        pdf,
        _blocks(),
        0,
        out_pdf,
        body_size=11.5,
        sidebar_size=9.5,
    )

    assert len(res.sidebar_bands) == 1
    band_x0, band_x1 = res.sidebar_bands[0]
    assert band_x0 < 1 and band_x1 >= SIDEBAR_X1 - 1
    assert res.sidebar_blocks == 3
    assert res.cross_leaks == []
    assert not [x for x in res.layouts if x.role == "sidebar" and x.overflow]

    doc = pymupdf.open(out_pdf)
    page = doc[0]
    side_words = page.get_text("words", clip=pymupdf.Rect(0, 0, SIDEBAR_X1, 800))
    doc.close()
    cjk = sum(
        1
        for word in side_words
        for ch in str(word[4])
        if "\u4e00" <= ch <= "\u9fff"
    )
    # 侧栏不再是「保留原文跳过」：这一列必须出现中文译文。
    assert cjk > 0


def test_sidebar_size_stays_in_its_own_range(tmp_path: Path) -> None:
    pdf = tmp_path / "side.pdf"
    _make_pdf(pdf)
    res = render_page(pdf, _blocks(), 0, None, body_size=12.0, sidebar_size=8.25)
    side_layouts = [x for x in res.layouts if x.role == "sidebar"]
    assert side_layouts
    for layout in side_layouts:
        assert SIDEBAR_MIN_SIZE - 0.01 <= layout.font_size <= SIDEBAR_MAX_SIZE + 0.01
        assert layout.font_size <= 8.25 + 0.01


def test_sidebar_translate_switch_keeps_original(
    tmp_path: Path, monkeypatch
) -> None:
    pdf = tmp_path / "side.pdf"
    _make_pdf(pdf)
    monkeypatch.setenv("PAPEREAD_SIDEBAR_TRANSLATE", "0")
    res = render_page(pdf, _blocks(), 0, None, body_size=11.5)
    # 关闭开关后回到 v23 行为：不认侧栏、不单独排版。
    assert res.sidebar_bands == []
    assert res.sidebar_blocks == 0


def test_build_report_written(tmp_path: Path) -> None:
    pdf = tmp_path / "side.pdf"
    _make_pdf(pdf)
    out = tmp_path / "out"
    result = build_translated_pdf(pdf, _blocks(), out, lang="zh-Hans", cache_key="v1")

    report_path = out / "build_report.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["algo_ver"] == result["build_report"]["algo_ver"]
    assert report["pages"] == 1
    assert report["page_sizes_ok"] is True
    assert report["cross_column_leaks"] == []
    assert report["sidebar_overflow"] == []
    assert set(report["coverage"]) == {"body", "sidebar", "table"}
    assert 0.0 <= report["coverage"]["body"] <= 1.0
    assert result["body_size_used"] > 0