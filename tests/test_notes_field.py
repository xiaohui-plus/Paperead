"""笔记领域页：摘要、跨会议统计与高频主题。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.notes_api as notes_api
from server.notes_api import build_router
from server.notes_catalog import NotesCatalog
from server.notes_search import NoteSearch
from server.notes_store import NoteStore

TREE = {
    "tree": [
        {"type": "blob", "path": "docs/CVPR2026/llm_reasoning/chain_of_thought_reasoning.md"},
        {"type": "blob", "path": "docs/CVPR2026/llm_reasoning/multi_agent_deliberation.md"},
        {"type": "blob", "path": "docs/ICLR2026/llm_reasoning/chain_of_thought_planning.md"},
    ]
}

FRONT = """---
title: >-
  [论文解读] Demo
description: >-
  一句话摘要：把概念分解成子概念后逐轮审议。
tags:
  - "CVPR 2026"
---

# Demo
"""


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "server.notes_catalog.http_get_text", lambda *a, **k: json.dumps(TREE)
    )
    monkeypatch.setattr(
        notes_api, "http_get_prefix_many", lambda urls, *a, **k: [FRONT for _ in urls]
    )

    store = NoteStore(tmp_path / "data")
    catalog = NotesCatalog(tmp_path)
    catalog._items = catalog._fetch()
    catalog._state = "ready"

    app = FastAPI()
    app.include_router(
        build_router(
            store,
            NoteSearch(store, tmp_path / "data" / "notes" / "search.db"),
            paper_store=None,
            parse_task=lambda paper_id: None,  # type: ignore[arg-type]
            public=lambda row: row,
            catalog=catalog,
        )
    )
    return TestClient(app)


def test_field_page_returns_list_across_and_themes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.get(
        "/api/notes/catalog/field",
        params={"conference": "CVPR2026", "field": "llm_reasoning", "limit": 30},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["matched"] == 2
    assert len(body["items"]) == 2
    assert {item["name"]: item["count"] for item in body["across"]} == {
        "CVPR2026": 2,
        "ICLR2026": 1,
    }
    words = {theme["word"] for theme in body["themes"]}
    assert "chain" in words and "thought" in words

    paths = ",".join(item["path"] for item in body["items"])
    descriptions = client.get(
        "/api/notes/catalog/descriptions", params={"paths": paths}
    ).json()["descriptions"]
    assert all(
        descriptions[item["path"]] == "一句话摘要：把概念分解成子概念后逐轮审议。"
        for item in body["items"]
    )


def test_descriptions_are_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    paths = (
        "docs/CVPR2026/llm_reasoning/chain_of_thought_reasoning.md,"
        "docs/CVPR2026/llm_reasoning/multi_agent_deliberation.md"
    )

    client.get("/api/notes/catalog/descriptions", params={"paths": paths})
    calls = {"count": 0}
    monkeypatch.setattr(
        notes_api,
        "http_get_prefix_many",
        lambda urls, *a, **k: calls.__setitem__("count", calls["count"] + 1)
        or [FRONT for _ in urls],
    )
    client.get("/api/notes/catalog/descriptions", params={"paths": paths})

    assert calls["count"] == 0
