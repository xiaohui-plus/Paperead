from __future__ import annotations

import asyncio
from typing import Any

from server.research import abstracts


class FakeStore:
    def __init__(self, row: dict[str, Any]):
        self.row = row

    def get(self, paper_id: str) -> dict[str, Any]:
        return self.row

    def read_blocks(self, paper_id: str) -> list[dict[str, Any]]:
        return [{"text": "Received 2024. doi: 10.1000/\nxyz"}]

    def update(self, paper_id: str, **patch: Any) -> dict[str, Any]:
        self.row.update(patch)
        return self.row


def test_find_doi_joins_broken_lines() -> None:
    assert abstracts.find_doi("doi: 10.1000/\nxyz .") == "10.1000/xyz"


def test_lookup_prefers_source_id(monkeypatch: Any) -> None:
    calls: list[str] = []

    async def fake_source(source_id: str) -> str:
        calls.append("source")
        return "from source"

    async def fake_doi(doi: str) -> str:
        calls.append("doi")
        return "from doi"

    monkeypatch.setattr(abstracts, "_from_source", fake_source)
    monkeypatch.setattr(abstracts, "_from_doi", fake_doi)
    result = asyncio.run(abstracts.lookup_abstract("Title", "arxiv:1", "10.1/x"))
    assert result == "from source"
    assert calls == ["source"]


def test_lookup_falls_back_to_title(monkeypatch: Any) -> None:
    async def empty_source(source_id: str) -> str:
        return ""

    async def empty_doi(doi: str) -> str:
        return ""

    async def by_title(title: str) -> str:
        return "from title"

    monkeypatch.setattr(abstracts, "_from_source", empty_source)
    monkeypatch.setattr(abstracts, "_from_doi", empty_doi)
    monkeypatch.setattr(abstracts, "_from_title", by_title)
    assert asyncio.run(abstracts.lookup_abstract("Title", "arxiv:1", "10.1/x")) == "from title"


def test_fill_one_skips_paper_with_abstract(monkeypatch: Any) -> None:
    async def boom(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("已有摘要的论文不应再查询")

    monkeypatch.setattr(abstracts, "lookup_abstract", boom)
    store = FakeStore({"id": "p1", "title": "T", "abstract": "已有摘要"})
    assert asyncio.run(abstracts.fill_one(store, "p1")) is False


def test_fill_one_uses_doi_from_first_pages(monkeypatch: Any) -> None:
    seen: dict[str, str] = {}

    async def fake_lookup(title: str, source_id: str = "", doi: str = "") -> str:
        seen.update(title=title, source_id=source_id, doi=doi)
        return "摘要正文"

    monkeypatch.setattr(abstracts, "lookup_abstract", fake_lookup)
    store = FakeStore({"id": "p1", "title": "A Paper", "source_id": ""})
    assert asyncio.run(abstracts.fill_one(store, "p1")) is True
    assert seen["doi"] == "10.1000/xyz"
    assert store.row["abstract"] == "摘要正文"
    assert store.row["abstract_source"] == "lookup"
