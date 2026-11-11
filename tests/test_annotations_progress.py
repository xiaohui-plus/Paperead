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


def test_annotation_pair_persists_and_updates(tmp_path: Path):
    store = _store(tmp_path)
    pair = {"source": "zh", "start": 0, "end": 11, "quote": "模型取得了很好的结果。"}

    row = store.add_annotation(
        "p1",
        {
            "block_id": "b0001",
            "source": "en",
            "start": 0,
            "end": 3,
            "quote": "The",
            "color": "yellow",
            "pair": pair,
        },
    )
    assert row["pair"] == pair

    updated = store.update_annotation(
        "p1", row["id"], {"pair": {"source": "zh", "start": 0, "end": 2, "quote": "模型"}}
    )
    assert updated is not None
    assert updated["pair"]["quote"] == "模型"

    # 旧数据没有 pair：读出来保持缺失，前端按「等译文再补」处理
    legacy = [
        {
            "id": "a_legacy",
            "block_id": "b0001",
            "source": "en",
            "start": 0,
            "end": 3,
            "quote": "The",
            "color": "yellow",
            "kind": "highlight",
            "note": "",
            "created_at": 1,
            "updated_at": 1,
        }
    ]
    store.annotations_path("p1").write_text(json.dumps(legacy), encoding="utf-8")
    assert "pair" not in store.list_annotations("p1")[0]


def test_annotation_pair_api(monkeypatch, tmp_path: Path):
    import importlib

    from fastapi.testclient import TestClient

    _store(tmp_path)
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.setenv("PAPEREAD_PORT", "8137")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)

    import server.main as main

    importlib.reload(main)

    pair = {"source": "zh", "start": 0, "end": 11, "quote": "模型取得了很好的结果。"}
    with TestClient(main.app) as client:
        created = client.post(
            "/api/papers/p1/annotations",
            json={
                "block_id": "b0001",
                "source": "en",
                "start": 0,
                "end": 3,
                "quote": "The",
                "color": "yellow",
                "pair": pair,
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["pair"] == pair

        # 对侧范围越界要被拒绝
        bad = client.post(
            "/api/papers/p1/annotations",
            json={
                "block_id": "b0001",
                "source": "en",
                "start": 0,
                "end": 3,
                "quote": "The",
                "pair": {"source": "zh", "start": 0, "end": 999, "quote": "x"},
            },
        )
        assert bad.status_code == 400

        # pair 必须来自另一侧
        same_side = client.post(
            "/api/papers/p1/annotations",
            json={
                "block_id": "b0001",
                "source": "en",
                "start": 0,
                "end": 3,
                "quote": "The",
                "pair": {"source": "en", "start": 0, "end": 3, "quote": "The"},
            },
        )
        assert same_side.status_code == 400

        # 先建再补：模拟「划线时还没译文，翻好后前端 PATCH 补 pair」
        hl = client.post(
            "/api/papers/p1/annotations",
            json={
                "block_id": "b0001",
                "source": "en",
                "start": 4,
                "end": 9,
                "quote": "model",
                "color": "green",
            },
        )
        assert hl.status_code == 201, hl.text
        assert hl.json()["pair"] is None
        patched = client.patch(
            f"/api/papers/p1/annotations/{hl.json()['id']}",
            json={"pair": {"source": "zh", "start": 0, "end": 2, "quote": "模型"}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["pair"]["quote"] == "模型"

        listed = client.get("/api/papers/p1/annotations")
        assert len(listed.json()["annotations"]) == 2

def test_pdf_page_annotation_api(monkeypatch, tmp_path: Path):
    import importlib

    from fastapi.testclient import TestClient

    _store(tmp_path)
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.setenv("PAPEREAD_PORT", "8138")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)

    import server.main as main

    importlib.reload(main)

    rects = [
        {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.02},
        {"x": 0.1, "y": 0.23, "w": 0.25, "h": 0.02},
    ]
    with TestClient(main.app) as client:
        created = client.post(
            "/api/papers/p1/annotations",
            json={
                "quote": "The model achieves strong results.",
                "source": "en",
                "color": "green",
                "kind": "highlight",
                "page": 1,
                "rects": rects,
            },
        )
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["block_id"] == ""
        assert row["page"] == 1
        assert row["rects"] == rects
        assert row["pair"] is None

        # 只改笔记/颜色不影响页内位置
        patched = client.patch(
            f"/api/papers/p1/annotations/{row['id']}",
            json={"note": "重点方法", "color": "blue"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["note"] == "重点方法"
        assert patched.json()["rects"] == rects

        # 没有选区、没有选中文字都要拒绝
        no_rects = client.post(
            "/api/papers/p1/annotations",
            json={"quote": "x", "page": 1},
        )
        assert no_rects.status_code == 400
        no_quote = client.post(
            "/api/papers/p1/annotations",
            json={"quote": "  ", "page": 1, "rects": rects},
        )
        assert no_quote.status_code == 400

        # 归一化坐标越界要拒绝
        bad = client.post(
            "/api/papers/p1/annotations",
            json={"quote": "x", "page": 1, "rects": [{"x": 1.2, "y": 0, "w": 0.1, "h": 0.1}]},
        )
        assert bad.status_code == 422

        # 段落标注仍要求合法 block 与偏移
        missing_block = client.post(
            "/api/papers/p1/annotations",
            json={"quote": "The", "source": "en", "start": 0, "end": 3},
        )
        assert missing_block.status_code == 400

        listed = client.get("/api/papers/p1/annotations")
        assert len(listed.json()["annotations"]) == 1

