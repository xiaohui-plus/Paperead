"""笔记一键导入 PDF：链接挑选、入库与笔记关联。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.notes_api as notes_api
from server.notes_api import build_router
from server.notes_search import NoteSearch
from server.notes_store import NoteStore

NOTE = """---
title: Demo
tags:
  - "CVPR 2026"
---

# Demo Paper

**论文**: [CVF](https://openaccess.thecvf.com/content/CVPR2026/html/X_CVPR_2026_paper.html)

## 一句话总结
演示用。

## 方法详解
略。
"""

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


class FakePaperStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.added: list[tuple[str, bytes]] = []

    def get(self, paper_id: str) -> dict[str, Any] | None:
        return self.rows.get(paper_id)

    def find_by_pdf_hash(self, data: bytes, folder_id: str | None = None) -> Any:
        del data, folder_id
        return None

    def add(self, filename: str, data: bytes, meta: Any = None) -> dict[str, Any]:
        del meta
        self.added.append((filename, data))
        row = {"id": "p_demo", "title": "Demo Paper", "status": "uploaded"}
        self.rows["p_demo"] = row
        return row


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docs = tmp_path / "docs" / "CVPR2026" / "llm_reasoning"
    docs.mkdir(parents=True)
    (docs / "demo.md").write_text(NOTE, encoding="utf-8")

    store = NoteStore(tmp_path / "data")
    from paperead.notes_import import collect_notes

    note = collect_notes(tmp_path / "docs")[0]
    saved = store.upsert(note)

    papers = FakePaperStore()
    parsed: list[str] = []

    async def parse_task(paper_id: str) -> None:
        parsed.append(paper_id)

    async def fake_download(url: str) -> bytes:
        assert url.endswith("_CVPR_2026_paper.pdf")
        return MINIMAL_PDF

    monkeypatch.setattr(notes_api, "_download_pdf", fake_download)

    app = FastAPI()
    app.include_router(
        build_router(
            store,
            NoteSearch(store, tmp_path / "data" / "notes" / "search.db"),
            paper_store=papers,
            parse_task=parse_task,
            public=lambda row: row,
        )
    )
    return TestClient(app), store, papers, saved["id"], parsed


def test_import_note_paper_downloads_and_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store, papers, note_id, parsed = _client(tmp_path, monkeypatch)

    response = client.post(f"/api/notes/{note_id}/import-paper")

    assert response.status_code == 200
    body = response.json()
    assert body["paper"]["id"] == "p_demo"
    assert body["created"] is True
    assert papers.added and papers.added[0][1] == MINIMAL_PDF
    assert parsed == ["p_demo"]

    saved = store.get(note_id)
    assert saved is not None
    assert saved["paper_id"] == "p_demo"


def test_import_note_paper_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, papers, note_id, _parsed = _client(tmp_path, monkeypatch)

    client.post(f"/api/notes/{note_id}/import-paper")
    second = client.post(f"/api/notes/{note_id}/import-paper")

    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert len(papers.added) == 1


def test_import_note_paper_without_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "plain.md").write_text("# 无链接笔记\n\n正文。\n", encoding="utf-8")

    store = NoteStore(tmp_path / "data")
    from paperead.notes_import import collect_notes

    saved = store.upsert(collect_notes(docs)[0])
    app = FastAPI()
    app.include_router(
        build_router(
            store,
            NoteSearch(store, tmp_path / "data" / "notes" / "search.db"),
            paper_store=FakePaperStore(),
            parse_task=lambda paper_id: None,  # type: ignore[arg-type]
            public=lambda row: row,
        )
    )

    response = TestClient(app).post(f"/api/notes/{saved['id']}/import-paper")

    assert response.status_code == 400
    assert "链接" in response.json()["detail"]