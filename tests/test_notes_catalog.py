"""笔记广场：在线目录构建、筛选分页与按需加载。"""

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
        {"type": "blob", "path": "docs/CVPR2026/llm_reasoning/agile_deliberation.md"},
        {"type": "blob", "path": "docs/CVPR2026/llm_reasoning/agent_planning.md"},
        {"type": "blob", "path": "docs/CVPR2026/llm_reasoning/index.md"},
        {"type": "blob", "path": "docs/ICLR2026/llm_agent/tool_use.md"},
        {"type": "blob", "path": "docs/assets/logo.md"},
        {"type": "blob", "path": "docs/README.md"},
        {"type": "tree", "path": "docs/CVPR2026/llm_reasoning"},
    ]
}

NOTE_MD = """---
title: >-
  [论文解读] Agile Deliberation
tags:
  - "CVPR 2026"
---

# Agile Deliberation

## 一句话总结
主观概念的人在回路框架。

## 方法详解
### 整体框架
流程说明。
"""


def _catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NotesCatalog:
    monkeypatch.setattr(
        "server.notes_catalog.http_get_text", lambda *a, **k: json.dumps(TREE)
    )
    catalog = NotesCatalog(tmp_path)
    catalog._items = catalog._fetch()
    catalog._state = "ready"
    return catalog


def test_catalog_filters_index_and_assets(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path, monkeypatch)

    assert len(catalog._items) == 3
    assert {item["conference"] for item in catalog._items} == {"CVPR2026", "ICLR2026"}
    assert all(not item["path"].endswith("index.md") for item in catalog._items)


def test_catalog_stats_search_and_paging(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path, monkeypatch)

    confs = {item["name"]: item["count"] for item in catalog.conferences()}
    assert confs == {"CVPR2026": 2, "ICLR2026": 1}
    assert {item["name"] for item in catalog.fields("CVPR2026")} == {"llm_reasoning"}

    hit = catalog.list(query="agile")
    assert hit["matched"] == 1
    assert hit["items"][0]["title"].startswith("Agile")

    page = catalog.list(conference="CVPR2026", limit=1, offset=1)
    assert page["matched"] == 2
    assert len(page["items"]) == 1


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = NoteStore(tmp_path / "data")
    catalog = _catalog(tmp_path, monkeypatch)
    monkeypatch.setattr(
        notes_api, "http_get_text_any", lambda *a, **k: NOTE_MD
    )
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
    return TestClient(app), store


def test_catalog_endpoint(tmp_path: Path, monkeypatch) -> None:
    client, _store = _client(tmp_path, monkeypatch)

    response = client.get("/api/notes/catalog", params={"limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["matched"] == 3
    assert len(body["items"]) == 2
    assert body["conferences"]


def test_online_note_parses_and_caches(tmp_path: Path, monkeypatch) -> None:
    client, store = _client(tmp_path, monkeypatch)
    path = "docs/CVPR2026/llm_reasoning/agile_deliberation.md"

    first = client.get("/api/notes/online", params={"path": path})

    assert first.status_code == 200
    note = first.json()["note"]
    assert first.json()["cached"] is False
    assert note["title"] == "Agile Deliberation"
    assert note["conference"] == "CVPR2026"
    assert len(note["blocks"]) > 0
    assert store.count() == 1

    second = client.get("/api/notes/online", params={"path": path})
    assert second.status_code == 200
    assert second.json()["cached"] is True


def test_online_rejects_path_traversal(tmp_path: Path, monkeypatch) -> None:
    client, _store = _client(tmp_path, monkeypatch)

    response = client.get("/api/notes/online", params={"path": "docs/../etc/passwd.md"})

    assert response.status_code == 400

def test_overview_groups_conferences_and_counts(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path, monkeypatch)

    data = catalog.overview()

    assert data["total"] == 3
    assert data["conferenceCount"] == 2
    assert data["fieldCount"] == 2
    card = next(item for item in data["conferences"] if item["name"] == "CVPR2026")
    assert card["count"] == 2
    assert card["fieldCount"] == 1
    group = next(item for item in card["groups"] if item["name"] == "LLM 与大模型")
    labels = {field["label"] for field in group["fields"]}
    assert "LLM 推理" in labels
    iclr = next(item for item in data["conferences"] if item["name"] == "ICLR2026")
    iclr_group = next(item for item in iclr["groups"] if item["name"] == "LLM 与大模型")
    assert {field["label"] for field in iclr_group["fields"]} == {"LLM 智能体"}


def test_list_items_carry_readable_label(tmp_path: Path, monkeypatch) -> None:
    catalog = _catalog(tmp_path, monkeypatch)

    items = catalog.list(conference="CVPR2026")["items"]

    assert items
    assert all(item["label"] for item in items)



CN_TREE = {
    "tree": [
        {"type": "blob", "path": "docs/CVPR2026/image_generation/diffusion_model_for_images.md"},
        {"type": "blob", "path": "docs/CVPR2026/segmentation/organ_segmentation_with_gan.md"},
        {"type": "blob", "path": "docs/ICLR2026/llm_reasoning/chain_of_thought_reasoning.md"},
        {"type": "blob", "path": "docs/ICLR2026/reinforcement_learning/reward_model_policy.md"},
        {"type": "blob", "path": "docs/CVPR2026/image_generation/index.md"},
    ]
}


def _cn_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NotesCatalog:
    monkeypatch.setattr(
        "server.notes_catalog.http_get_text", lambda *a, **k: json.dumps(CN_TREE)
    )
    catalog = NotesCatalog(tmp_path)
    catalog._items = catalog._fetch()
    catalog._state = "ready"
    return catalog


def test_chinese_query_expands_to_english_candidates(tmp_path: Path, monkeypatch) -> None:
    catalog = _cn_catalog(tmp_path, monkeypatch)

    hit = catalog.list(query="扩散模型")

    assert hit["matched"] == 1
    assert "diffusion" in hit["items"][0]["title"].lower()
    assert hit["query"]["concepts"] == ["扩散模型"]
    assert {"term": "diffusion", "source": "扩散模型"} in hit["query"]["terms"]
    assert "diffusion" in hit["items"][0]["hits"]


def test_any_concept_hit_counts_and_sorts_by_coverage(tmp_path: Path, monkeypatch) -> None:
    catalog = _cn_catalog(tmp_path, monkeypatch)

    hit = catalog.list(query="扩散模型 语义分割")

    # 任一概念命中即返回：扩散那篇命中 1/2，分割那篇命中 1/2。
    assert hit["matched"] == 2
    titles = {item["title"] for item in hit["items"]}
    assert any("Diffusion" in title for title in titles)
    assert any("Organ" in title for title in titles)


def test_english_match_uses_whole_words_not_letters(tmp_path: Path, monkeypatch) -> None:
    catalog = _cn_catalog(tmp_path, monkeypatch)

    assert catalog.list(query="gan")["matched"] == 1
    assert catalog.list(query="organ")["matched"] == 1
    # “ga” 不是完整单词，不能靠字母子串命中 GAN。
    assert catalog.list(query="ga")["matched"] == 0
    # reasoning 不能被子串 reason 命中。
    assert catalog.list(query="reason")["matched"] == 0
    assert catalog.list(query="reasoning")["matched"] == 1


def test_chinese_field_label_is_searchable(tmp_path: Path, monkeypatch) -> None:
    catalog = _cn_catalog(tmp_path, monkeypatch)

    hit = catalog.list(query="强化学习")

    assert hit["matched"] >= 1
    item = hit["items"][0]
    assert item["field"] == "reinforcement_learning"
    assert item["hits"]


def test_catalog_endpoint_expands_chinese_query(tmp_path: Path, monkeypatch) -> None:
    client, _store = _client(tmp_path, monkeypatch)

    response = client.get("/api/notes/catalog", params={"q": "推理"})

    assert response.status_code == 200
    body = response.json()
    assert body["matched"] == 2
    assert body["query"]["concepts"] == ["推理"]
    assert all(item["field"] == "llm_reasoning" for item in body["items"])
    assert all("reasoning" in item["hits"] for item in body["items"])
