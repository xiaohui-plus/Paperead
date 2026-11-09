from __future__ import annotations

import asyncio
import json
from pathlib import Path

import paperead.highlights as highlights
from paperead.layout import Block
from server.store import LibraryStore


def _block(block_id: str = "b0002") -> Block:
    return Block(
        id=block_id,
        order=2,
        page=3,
        kind="para",
        text="To solve this, we propose GraphNet and evaluate it on three datasets.",
        bbox=(0, 0, 100, 10),
        translated="为了解决这个问题，我们提出 GraphNet，并在三个数据集上评估。",
    )


def test_extract_highlights_keeps_only_exact_quotes(monkeypatch):
    block = _block()

    async def fake_chat(**_kwargs):
        return json.dumps(
            {
                "highlights": [
                    {
                        "block_id": "b0002",
                        "category": "objective",
                        "quote_en": "we propose GraphNet",
                        "quote_zh": "我们提出 GraphNet",
                    },
                    {
                        "block_id": "b0002",
                        "category": "method",
                        "quote_en": "evaluate it on three datasets",
                        "quote_zh": "在三个数据集上评估",
                    },
                    {
                        # 模型改写过的句子：找不到原文，必须丢弃
                        "block_id": "b0002",
                        "category": "conclusion",
                        "quote_en": "GraphNet works better than all baselines",
                        "quote_zh": "GraphNet 优于所有基线",
                    },
                    {
                        "block_id": "b0002",
                        "category": "objective",
                        "quote_en": "To solve this",
                        "quote_zh": "为了解决这个问题",
                    },
                    {
                        "block_id": "b0002",
                        "category": "not-a-category",
                        "quote_en": "three datasets",
                        "quote_zh": "三个数据集",
                    },
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(highlights, "_translate_chat", fake_chat)
    rows = asyncio.run(highlights.extract_highlights([block]))

    assert len(rows) == 2
    assert {row["category"] for row in rows} == {"objective", "method"}
    first = next(row for row in rows if row["category"] == "objective")
    assert first["start"] == block.text.find(first["quote_en"])
    assert first["zh_start"] == block.translated.find(first["quote_zh"])
    assert first["quote_en"] in block.text
    assert first["quote_zh"] in block.translated


def test_extract_highlights_returns_empty_when_no_translation(monkeypatch):
    block = _block()
    block.translated = ""

    async def fake_chat(**_kwargs):
        raise AssertionError("没有译文时不应该调用模型")

    monkeypatch.setattr(highlights, "_translate_chat", fake_chat)
    assert asyncio.run(highlights.extract_highlights([block])) == []


def _store(tmp_path: Path) -> LibraryStore:
    store = LibraryStore(tmp_path / "lib")
    store.index.write_text(
        json.dumps(
            [
                {
                    "id": "p1",
                    "title": "Paper p1",
                    "authors": "",
                    "filename": "p1.pdf",
                    "pages": 3,
                    "size": 1024,
                    "created_at": 1,
                    "status": "ready",
                    "error": "",
                    "translated": 0,
                    "blocks": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    return store


def test_store_replaces_page_highlights(tmp_path: Path):
    store = _store(tmp_path)
    row = {
        "block_id": "b0002",
        "category": "conclusion",
        "quote_en": "three datasets",
        "quote_zh": "三个数据集",
        "start": 60,
        "end": 74,
        "zh_start": 20,
        "zh_end": 27,
    }
    added = store.replace_page_highlights("p1", 3, [row])
    assert len(added) == 1
    assert store.highlight_pages("p1")["3"]["count"] == 1
    assert store.list_highlights("p1")[0]["page"] == 3

    # 同一页重跑：旧条目被替换，不会累加
    store.replace_page_highlights("p1", 3, [])
    assert store.list_highlights("p1") == []
    assert store.highlight_pages("p1")["3"]["count"] == 0

    store.clear_highlights("p1")
    assert not store.highlights_path("p1").exists()
