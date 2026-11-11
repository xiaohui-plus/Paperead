from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.research import explorer
from server.research.api import build_router
from server.research.files import save_bytes_in
from server.research.store import ResearchStore


@pytest.fixture()
def store(tmp_path: Path) -> ResearchStore:
    return ResearchStore(tmp_path / "research")


@pytest.fixture()
def client(store: ResearchStore):
    app = FastAPI()
    app.include_router(build_router(store))
    with TestClient(app) as test_client:
        yield test_client


def test_project_can_bind_local_folder(tmp_path: Path, store: ResearchStore):
    work = tmp_path / "电脑上的论文文件夹"
    (work / "草稿").mkdir(parents=True)
    (work / "草稿" / "第一章.md").write_text("# 第一章", encoding="utf-8")

    project = store.create_project(
        "review", "综述项目", root_path=str(work)
    )

    assert project["root_path"] == str(work.resolve())
    assert store.workspace_root(project["id"]) == work.resolve()

    relative, size = save_bytes_in(
        store.workspace_root(project["id"]), "笔记.md", b"# note"
    )
    item = store.add_file(project["id"], "笔记.md", relative, size=size)
    assert store.item_path(project["id"], item) == work.resolve() / "笔记.md"
    assert (work / "笔记.md").read_text(encoding="utf-8") == "# note"


def test_root_rejects_file_and_drive(tmp_path: Path, store: ResearchStore):
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        store.create_project("research", "错误", root_path=str(file_path))

    project = store.create_project("research", "默认")
    with pytest.raises(ValueError):
        store.update_project(project["id"], root_path=str(file_path))

    if Path("C:\\").parent == Path("C:\\"):
        with pytest.raises(ValueError):
            store.update_project(project["id"], root_path="C:\\")

    cleared = store.update_project(project["id"], root_path="")
    assert cleared is not None and cleared["root_path"] == ""
    assert store.workspace_root(project["id"]) == (
        store.workspace(project["id"]) / "files"
    ).resolve()


def test_project_explorer_lists_and_reads_files(
    tmp_path: Path, store: ResearchStore, client: TestClient
):
    work = tmp_path / "工作区"
    (work / "子目录").mkdir(parents=True)
    (work / "子目录" / "正文.md").write_text("你好", encoding="utf-8")
    (work / "readme.txt").write_text("root", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    project = store.create_project("research", "浏览", root_path=str(work))
    project_id = project["id"]

    root = client.get(f"/api/research/projects/{project_id}/explorer")
    assert root.status_code == 200
    names = [item["name"] for item in root.json()["items"]]
    assert names == ["子目录", "readme.txt"]
    assert root.json()["path"] == ""

    sub = client.get(
        f"/api/research/projects/{project_id}/explorer",
        params={"path": "子目录"},
    )
    assert sub.status_code == 200
    assert sub.json()["parent"] == ""
    assert sub.json()["items"][0]["path"] == "子目录/正文.md"

    content = client.get(
        f"/api/research/projects/{project_id}/explorer/file",
        params={"path": "子目录/正文.md"},
    )
    assert content.status_code == 200
    assert content.content.decode("utf-8") == "你好"

    escaped = client.get(
        f"/api/research/projects/{project_id}/explorer/file",
        params={"path": "../outside.txt"},
    )
    assert escaped.status_code == 404


def test_fs_browse_and_roots(tmp_path: Path, client: TestClient):
    folder = tmp_path / "任意位置"
    folder.mkdir()
    (folder / "文档.md").write_text("x", encoding="utf-8")

    roots = client.get("/api/research/fs/roots")
    assert roots.status_code == 200
    assert isinstance(roots.json()["items"], list)

    listing = client.get("/api/research/fs/list", params={"path": str(folder)})
    assert listing.status_code == 200
    assert listing.json()["path"] == str(folder.resolve())
    assert [item["name"] for item in listing.json()["items"]] == ["文档.md"]

    missing = client.get(
        "/api/research/fs/list", params={"path": str(tmp_path / "不存在")}
    )
    assert missing.status_code == 404


def test_set_project_root_api(tmp_path: Path, store: ResearchStore, client: TestClient):
    work = tmp_path / "新工作区"
    work.mkdir()
    project = store.create_project("research", "切换")

    response = client.patch(
        f"/api/research/projects/{project['id']}/root",
        json={"path": str(work)},
    )
    assert response.status_code == 200
    assert response.json()["rootPath"] == str(work.resolve())

    reset = client.patch(
        f"/api/research/projects/{project['id']}/root",
        json={"path": ""},
    )
    assert reset.status_code == 200
    assert reset.json()["rootPath"] == ""


def test_agent_file_listing_skips_noise(tmp_path: Path):
    work = tmp_path / "仓库"
    (work / "src").mkdir(parents=True)
    (work / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (work / ".git").mkdir()
    (work / ".git" / "config").write_text("x", encoding="utf-8")
    (work / "node_modules").mkdir()
    (work / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")

    files = explorer.project_files(work)
    paths = [item["path"] for item in files]
    assert "src/main.py" in paths
    assert all(".git" not in path for path in paths)
    assert all("node_modules" not in path for path in paths)