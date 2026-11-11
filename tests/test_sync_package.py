from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.store import LibraryStore
from server.sync_package import build_router


@pytest.fixture()
def store(tmp_path: Path) -> LibraryStore:
    return LibraryStore(tmp_path / "data")


@pytest.fixture()
def client(store: LibraryStore, tmp_path: Path):
    app = FastAPI()
    app.include_router(build_router(store, tmp_path / "data"))
    with TestClient(app) as test_client:
        yield test_client


def make_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Sync package")
    data = doc.tobytes()
    doc.close()
    return data


def add_paper(store: LibraryStore, title: str = "同步论文", ready: bool = True) -> str:
    row = store.add("paper.pdf", make_pdf(), {"title": title})
    paper_id = str(row["id"])
    if ready:
        store.update(paper_id, status="ready", pages=1)
        store.blocks_path(paper_id).write_text(
            json.dumps(
                {
                    "title": title,
                    "page_count": 1,
                    "blocks": [{"type": "text", "text": "hello"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return paper_id


def test_export_and_download_package(store: LibraryStore, client: TestClient):
    paper_id = add_paper(store)

    response = client.post("/api/sync/export", json={"ids": [paper_id]})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["skipped"] == []
    path = Path(data["path"])
    assert path.is_file()
    assert data["size"] == path.stat().st_size

    download = client.get(data["url"])
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "paperead-sync"
        assert manifest["version"] == 1
        assert len(manifest["papers"]) == 1
        paper = manifest["papers"][0]
        assert paper["id"] == paper_id
        assert paper["title"] == "同步论文"
        assert archive.read(paper["files"]["blocks"])
        assert paper["files"]["pdf"] in archive.namelist()
        assert f"papers/{paper_id}/pages/1.png" in archive.namelist()


def test_export_skips_unready_papers(store: LibraryStore, client: TestClient):
    ready_id = add_paper(store)
    pending_id = str(
        store.add("pending.pdf", make_pdf(), {"title": "还没解析"})["id"]
    )

    response = client.post(
        "/api/sync/export", json={"ids": [ready_id, pending_id]}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert [item["id"] for item in data["skipped"]] == [pending_id]
    assert data["skipped"][0]["reason"] == "还在解析"


def test_export_without_ready_papers_is_rejected(client: TestClient):
    response = client.post("/api/sync/export", json={"ids": []})
    assert response.status_code == 400


def test_download_rejects_path_escape(client: TestClient):
    response = client.get("/api/sync/export/..%2Fsecret.paperead")
    assert response.status_code == 404

    response = client.get("/api/sync/export/not-a-package.txt")
    assert response.status_code == 404