from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.research.search import LibrarySearch
from server.research.search_api import build_router
from server.store import LibraryStore


def make_library(tmp_path: Path) -> LibraryStore:
    library = LibraryStore(tmp_path / "lib")
    library.index.write_text(
        json.dumps(
            [
                {
                    "id": "p1",
                    "title": "肝癌免疫治疗耐药机制",
                    "authors": ["张三", "李四"],
                    "pages": 4,
                    "abstract": "本文综述肝癌免疫治疗耐药的机制与干预策略。",
                },
                {
                    "id": "p2",
                    "title": "CRISPR delivery systems",
                    "authors": ["Wang"],
                    "pages": 2,
                    "abstract": "Viral and non-viral vectors for CRISPR delivery.",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    library.write_blocks(
        "p1",
        [
            {"id": "b1", "page": 1, "type": "heading", "text": "肝癌免疫治疗耐药机制"},
            {"id": "b2", "page": 1, "type": "paragraph", "text": "免疫检查点抑制剂在部分患者中无效，耐药机制包括肿瘤微环境抑制。"},
            {"id": "b3", "page": 1, "type": "paragraph", "text": "另一段同样提到耐药但位于同一页。"},
            {"id": "b4", "page": 2, "type": "paragraph", "text": "联合治疗可以改善耐药，但仍需生物标志物筛选。"},
        ],
    )
    library.write_blocks(
        "p2",
        [{"id": "c1", "page": 1, "type": "paragraph", "text": "Lipid nanoparticles deliver CRISPR components."}],
    )
    return library


@pytest.fixture()
def library(tmp_path: Path) -> LibraryStore:
    return make_library(tmp_path)


@pytest.fixture()
def search(library: LibraryStore, tmp_path: Path) -> LibrarySearch:
    return LibrarySearch(library, tmp_path / "search.db")


def test_search_hits_full_text_with_pages(search: LibrarySearch):
    results = search.search("耐药")
    assert results
    assert results[0]["paperId"] == "p1"
    assert {row["page"] for row in results if row["page"]} >= {1}

    # 同一页最多两段，避免一段话刷屏。
    same_page = [row for row in results if row["paperId"] == "p1" and row["page"] == 1]
    assert len(same_page) <= 2


def test_search_dedupes_same_paper(search: LibrarySearch):
    results = search.search("耐药", limit=10)
    ids = [row["paperId"] for row in results]
    assert ids
    assert len(ids) == len(set(ids))


def test_search_returns_abstract_excerpt(search: LibrarySearch):
    hits = search.search("耐药")
    assert hits
    assert hits[0]["hasAbstract"] is True
    assert hits[0]["abstract"]


def test_search_multiword_uses_or(search: LibrarySearch):
    # 多词查询按词 OR，避免长英文查询因整句精确匹配而 0 命中。
    hits = search.search("CRISPR lipid")
    assert hits
    assert hits[0]["paperId"] == "p2"


def test_search_english_and_short_query(search: LibrarySearch):
    assert search.search("CRISPR")[0]["paperId"] in {"p1", "p2"}
    # 短于 3 个字符时走 LIKE 回退。
    hits = search.search("免疫")
    assert hits and all("免疫" in hit["snippet"] or "免疫" in hit["title"] for hit in hits)


def test_index_refreshes_when_library_changes(library: LibraryStore, search: LibrarySearch):
    assert search.search("耐药")
    library.write_blocks(
        "p1",
        [{"id": "b9", "page": 4, "type": "paragraph", "text": "新增的段落讨论完全不同的主题。"}],
    )
    assert search.search("完全不同的主题")


def test_read_paper_page_and_limits(library: LibraryStore, search: LibrarySearch):
    data = search.read_paper("p1", page=2)
    assert data is not None
    assert [block["page"] for block in data["blocks"]] == [2]

    # max_chars 下限是 500，先塞一段超长正文再验证截断。
    library.write_blocks(
        "p1",
        [{"id": "b9", "page": 1, "type": "paragraph", "text": "耐" * 900}],
    )
    full = search.read_paper("p1", max_chars=500)
    assert full is not None
    assert full["truncated"] is True
    assert len("".join(block["text"] for block in full["blocks"])) <= 500

    assert search.read_paper("missing") is None


def test_read_paper_needs_ingest(library: LibraryStore, search: LibrarySearch):
    library.index.write_text(
        json.dumps([{"id": "p3", "title": "未解析", "authors": [], "pages": 1}]),
        encoding="utf-8",
    )
    data = search.read_paper("p3")
    assert data is not None
    assert data["needsIngest"] is True


def test_internal_api_token_and_shapes(search: LibrarySearch, monkeypatch):
    app = FastAPI()
    app.include_router(build_router(search))

    with TestClient(app) as client:
        ok = client.get("/internal/research/search/library", params={"q": "耐药"})
        assert ok.status_code == 200
        assert ok.json()["results"]

        read = client.get("/internal/research/papers/p1/read", params={"page": 1})
        assert read.status_code == 200
        assert read.json()["title"] == "肝癌免疫治疗耐药机制"

        assert client.get("/internal/research/papers/nope/read").status_code == 404

        monkeypatch.setenv("PAPEREAD_INTERNAL_TOKEN", "secret")
        assert client.get("/internal/research/search/library", params={"q": "耐药"}).status_code == 401
        guarded = client.get(
            "/internal/research/search/library",
            params={"q": "耐药"},
            headers={"x-internal-token": "secret"},
        )
        assert guarded.status_code == 200
