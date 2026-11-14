"""agent 下载论文：进文献库 + 复制到当前项目工作文件夹。

用户没有指定目录时默认 ``<项目工作文件夹>/papers/``；也可以指定工作文件夹
内的子目录，但不能用绝对路径或跳出项目文件夹。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.research.store import ResearchStore
from server.research.tools_api import build_router

PDF_BYTES = b"%PDF-1.4\n% test fixture\n"


class FakeLibrary:
    def __init__(self, root: Path):
        self.root = root
        self.rows: dict[str, dict] = {}
        self.seq = 0

    def add_pdf(self, filename: str, data: bytes, meta: dict) -> dict:
        self.seq += 1
        paper_id = f"p_{self.seq}"
        papers = self.root / "papers"
        papers.mkdir(parents=True, exist_ok=True)
        (papers / f"{paper_id}.pdf").write_bytes(data)
        row = {
            "id": paper_id,
            "filename": filename,
            "title": meta.get("title") or Path(filename).stem,
            "size": len(data),
            **meta,
        }
        self.rows[paper_id] = row
        return row

    def get(self, paper_id: str):
        return self.rows.get(paper_id)

    def list(self):
        return list(self.rows.values())

    def find_by_source(self, source_id: str, project_id=None):
        return next(
            (row for row in self.rows.values() if row.get("source_id") == source_id),
            None,
        )

    def pdf_path(self, paper_id: str):
        path = self.root / "papers" / f"{paper_id}.pdf"
        return path if path.exists() else None


async def fake_import_paper(store, add_pdf, public, paper_id, project_id=None):
    row = add_pdf(
        f"{paper_id.split(':')[-1]}.pdf",
        PDF_BYTES,
        {"source": "arxiv", "source_id": paper_id, "title": "Test Paper"},
    )
    return {"paper": public(row), "imported": True, "already": False}


@pytest.fixture()
def make_workspace(tmp_path: Path, monkeypatch):
    def _make(root_path: Path | None = None):
        research = ResearchStore(tmp_path / "research")
        project = research.create_project(
            "research", "论文导入测试", root_path=str(root_path) if root_path else None
        )
        session = research.create_session(project["id"])
        library = FakeLibrary(tmp_path / "lib")
        monkeypatch.setattr(
            "server.research.tools_api.import_paper", fake_import_paper
        )
        app = FastAPI()
        app.include_router(
            build_router(
                research, library, library.add_pdf, lambda row: row
            )
        )
        client = TestClient(app)
        return client, research, project, session, library

    return _make


def _import(client, session, payload: dict):
    return client.post(
        f"/internal/research/sessions/{session['id']}/papers/import", json=payload
    )


def test_default_import_lands_in_project_papers(make_workspace):
    client, research, project, session, library = make_workspace()

    response = _import(client, session, {"id": "arxiv:2401.00001"})
    assert response.status_code == 200
    body = response.json()

    assert body["folder"] == "papers"
    assert body["saved"] == "papers/2401.00001.pdf"
    assert body["projectScoped"] is False
    root = Path(research.workspace_root(project["id"]))
    assert (root / "papers" / "2401.00001.pdf").read_bytes() == PDF_BYTES
    # 文献库仍有独立主副本，项目文件被移动/删除不影响阅读。
    assert library.pdf_path(body["paper"]["id"]) is not None
    items = research.list_items(project["id"])
    papers_folder = next(
        item
        for item in items
        if item["kind"] == "folder" and item["path"] == "papers"
    )
    paper_item = next(
        item for item in items if item.get("path") == "papers/2401.00001.pdf"
    )
    assert paper_item["parent_id"] == papers_folder["id"]


def test_import_uses_opened_workspace_folder(make_workspace, tmp_path: Path):
    work = tmp_path / "opened-folder"
    work.mkdir()
    client, research, project, session, _library = make_workspace(work)

    body = _import(client, session, {"id": "arxiv:2401.00002"}).json()

    assert body["saved"] == "papers/2401.00002.pdf"
    assert (work / "papers" / "2401.00002.pdf").read_bytes() == PDF_BYTES
    assert not (Path(project["workspace"]) / "files" / "papers").exists()


def test_import_accepts_subfolder_inside_workspace(make_workspace):
    client, research, project, session, _library = make_workspace()

    body = _import(
        client, session, {"id": "arxiv:2401.00003", "folder": "drafts/pdf"}
    ).json()

    assert body["folder"] == "drafts/pdf"
    assert body["saved"] == "drafts/pdf/2401.00003.pdf"
    root = Path(research.workspace_root(project["id"]))
    assert (root / "drafts" / "pdf" / "2401.00003.pdf").exists()


def test_import_rejects_paths_outside_workspace(make_workspace, tmp_path: Path):
    client, research, project, session, _library = make_workspace()
    root = Path(research.workspace_root(project["id"]))

    escaped = _import(
        client, session, {"id": "arxiv:2401.00004", "folder": "../escape"}
    ).json()
    absolute = _import(
        client, session, {"id": "arxiv:2401.00004", "folder": str(tmp_path / "abs")}
    ).json()

    assert "项目工作文件夹" in escaped["error"]
    assert "项目工作文件夹" in absolute["error"]
    assert not (root.parent / "escape").exists()
    assert not (tmp_path / "abs").exists()


def test_reimport_does_not_duplicate_file(make_workspace):
    client, research, project, session, library = make_workspace()

    first = _import(client, session, {"id": "arxiv:2401.00005"}).json()
    second = _import(client, session, {"id": "arxiv:2401.00005"}).json()

    assert first["saved"] == second["saved"]
    assert second["copied"] is False
    assert second["already"] is True
    root = Path(research.workspace_root(project["id"]))
    files = sorted((root / "papers").glob("*.pdf"))
    assert [item.name for item in files] == ["2401.00005.pdf"]
    assert len(library.list()) == 1