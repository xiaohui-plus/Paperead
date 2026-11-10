from __future__ import annotations

import json
from pathlib import Path

from paperead.layout import _continues_paragraph
from server.store import LibraryStore


def _store(tmp_path: Path) -> LibraryStore:
    store = LibraryStore(tmp_path / "lib")
    rows = [
        {
            "id": "p1",
            "title": "Paper p1",
            "authors": "",
            "filename": "p1.pdf",
            "pages": 1,
            "size": 1024,
            "created_at": 1,
            "status": "ready",
            "error": "",
            "translated": 0,
            "blocks": 0,
        }
    ]
    store.index.write_text(json.dumps(rows), encoding="utf-8")
    store.write_blocks(
        "p1",
        [
            {
                "id": "b0001",
                "order": 1,
                "page": 1,
                "kind": "para",
                "level": 0,
                "section": "",
                "text": "The model achieves strong results.",
                "translated": "模型取得了很好的结果。",
                "image_path": None,
                "table_html": "",
                "caption": "",
                "translatable": True,
                "bbox": [0, 0, 100, 10],
            }
        ],
    )
    return store


def test_annotation_lifecycle(tmp_path: Path):
    store = _store(tmp_path)

    row = store.add_annotation(
        "p1",
        {
            "block_id": "b0001",
            "source": "en",
            "start": 4,
            "end": 9,
            "quote": "model",
            "color": "yellow",
            "note": "",
        },
    )
    assert row["id"].startswith("a_")
    assert store.list_annotations("p1") == [row]

    updated = store.update_annotation("p1", row["id"], {"note": "关键方法"})
    assert updated is not None
    assert updated["note"] == "关键方法"
    assert updated["color"] == "yellow"

    assert store.delete_annotation("p1", row["id"]) is True
    assert store.list_annotations("p1") == []


def test_annotation_kind_defaults_and_persists(tmp_path: Path):
    store = _store(tmp_path)

    # 不传 kind：老客户端/老数据按荧光笔处理
    hl = store.add_annotation(
        "p1",
        {
            "block_id": "b0001",
            "source": "en",
            "start": 0,
            "end": 3,
            "quote": "The",
            "color": "yellow",
        },
    )
    assert hl["kind"] == "highlight"

    # 批注：彩色下划线
    cm = store.add_annotation(
        "p1",
        {
            "block_id": "b0001",
            "source": "en",
            "start": 4,
            "end": 9,
            "quote": "model",
            "color": "blue",
            "kind": "comment",
            "note": "这里的 baseline 有疑问",
        },
    )
    assert cm["kind"] == "comment"
    assert [row["kind"] for row in store.list_annotations("p1")] == ["highlight", "comment"]


def test_legacy_annotations_read_as_highlight(tmp_path: Path):
    store = _store(tmp_path)
    legacy = [
        {
            "id": "a_old",
            "block_id": "b0001",
            "source": "en",
            "start": 0,
            "end": 3,
            "quote": "The",
            "color": "green",
            "note": "",
            "created_at": 1,
            "updated_at": 1,
        }
    ]
    store.annotations_path("p1").write_text(json.dumps(legacy), encoding="utf-8")
    assert store.list_annotations("p1")[0]["kind"] == "highlight"


def test_removing_paper_cleans_annotations(tmp_path: Path):
    store = _store(tmp_path)
    store.add_annotation(
        "p1",
        {
            "block_id": "b0001",
            "source": "zh",
            "start": 0,
            "end": 2,
            "quote": "模型",
            "color": "blue",
        },
    )
    assert store.annotations_path("p1").exists()

    assert store.remove("p1") is True
    assert not store.annotations_path("p1").exists()


def test_cross_page_paragraph_detection():
    # 上一段没说完 + 本行小写开头 → 同一段
    assert _continues_paragraph(
        "the model achieves", "state-of-the-art results on all benchmarks"
    )
    # 上一行停在逗号上，下一行大写也允许
    assert _continues_paragraph("as shown in Fig. 2,", "The performance improves")
    # 上一段已经结束 → 断开
    assert not _continues_paragraph(
        "This is a complete sentence.", "next paragraph starts here"
    )
    # 没有接续证据的大写开头 → 宁可断开
    assert not _continues_paragraph("Results show that", "We next evaluate")
