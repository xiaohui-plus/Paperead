from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.research.api import build_router
from server.research.files import resolve_in_files, save_bytes
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


def test_projects_scoped_by_kind(store: ResearchStore):
    research = store.create_project("research", "肝癌免疫治疗")
    review = store.create_project("review", "综述草稿")

    assert [row["id"] for row in store.list_projects("research")] == [research["id"]]
    assert [row["id"] for row in store.list_projects("review")] == [review["id"]]
    assert {row["id"] for row in store.list_projects()} == {research["id"], review["id"]}
    assert store.list_projects("research")[0]["kind"] == "research"

    with pytest.raises(ValueError):
        store.list_projects("unknown")
    with pytest.raises(ValueError):
        store.create_project("unknown")


def test_default_name_and_rename(store: ResearchStore):
    project = store.create_project("research")
    assert project["name"] == "新项目"

    renamed = store.update_project(project["id"], name="   ")
    assert renamed is not None
    assert renamed["name"] == "新项目"

    renamed = store.update_project(project["id"], name="  真实项目名  ")
    assert renamed is not None
    assert renamed["name"] == "真实项目名"
    assert renamed["updated_at"] >= project["updated_at"]

    archived = store.update_project(project["id"], archived=True)
    assert archived is not None and archived["archived"] == 1
    assert store.list_projects("research") == []
    assert len(store.list_projects("research", include_archived=True)) == 1


def test_folder_tree_and_cascade_remove(store: ResearchStore):
    project = store.create_project("research", "项目")
    workspace = store.workspace(project["id"])

    folder = store.create_folder(project["id"], "文献")
    child = store.create_folder(project["id"], "综述", folder["id"])
    relative, size = save_bytes(workspace, "hcc.pdf", b"%PDF-1.4", folder["path"])
    item = store.add_file(
        project["id"],
        "hcc.pdf",
        relative,
        parent_id=folder["id"],
        mime="application/pdf",
        size=size,
        kind="paper",
    )

    items = store.list_items(project["id"])
    assert {row["id"] for row in items} == {folder["id"], child["id"], item["id"]}
    assert item["parent_id"] == folder["id"]
    assert item["path"] == "文献/hcc.pdf"
    assert store.item_path(project["id"], item).read_bytes() == b"%PDF-1.4"

    assert store.remove_item(folder["id"]) is True
    assert store.list_items(project["id"]) == []
    assert not store.item_path(project["id"], item).exists()


def test_rename_move_and_copy_items(store: ResearchStore):
    project = store.create_project("research", "项目")
    workspace = store.workspace(project["id"])

    folder = store.create_folder(project["id"], "文献")
    sub = store.create_folder(project["id"], "综述", folder["id"])
    relative, size = save_bytes(workspace, "a.pdf", b"%PDF-1.4", folder["path"])
    item = store.add_file(
        project["id"],
        "a.pdf",
        relative,
        parent_id=folder["id"],
        mime="application/pdf",
        size=size,
        kind="paper",
    )

    # 重命名文件夹：子文件夹与文件路径、磁盘目录一起更新。
    rows = store.update_item(project["id"], folder["id"], name="资料")
    renamed = next(row for row in rows if row["id"] == folder["id"])
    assert renamed["title"] == "资料" and renamed["path"] == "资料"
    assert next(row for row in rows if row["id"] == sub["id"])["path"] == "资料/综述"
    assert next(row for row in rows if row["id"] == item["id"])["path"] == "资料/a.pdf"
    assert (workspace / "files" / "资料" / "a.pdf").is_file()

    # 移动到根目录，再移动到子文件夹。
    rows = store.update_item(project["id"], item["id"], parent_id=None)
    moved = next(row for row in rows if row["id"] == item["id"])
    assert moved["parent_id"] is None and moved["path"] == "a.pdf"
    assert (workspace / "files" / "a.pdf").is_file()

    rows = store.update_item(project["id"], item["id"], parent_id=sub["id"])
    moved = next(row for row in rows if row["id"] == item["id"])
    assert moved["path"] == "资料/综述/a.pdf"
    assert (workspace / "files" / "资料" / "综述" / "a.pdf").is_file()

    # 不允许把文件夹移动到自己的子孙目录。
    with pytest.raises(ValueError):
        store.update_item(project["id"], folder["id"], parent_id=sub["id"])

    # 复制文件与文件夹（文件夹递归复制，磁盘文件一起复制）。
    rows = store.copy_item(project["id"], item["id"], folder["id"])
    file_copy = next(
        row
        for row in rows
        if row["parent_id"] == folder["id"] and row["kind"] == "paper"
    )
    assert file_copy["id"] != item["id"]
    assert file_copy["path"] == "资料/a.pdf"
    assert store.item_path(project["id"], file_copy).read_bytes() == b"%PDF-1.4"

    rows = store.copy_item(project["id"], folder["id"], None)
    folder_copy = next(
        row
        for row in rows
        if row["parent_id"] is None and row["title"] == "资料-1"
    )
    assert folder_copy["kind"] == "folder"
    assert (workspace / "files" / "资料-1" / "综述" / "a.pdf").is_file()

    assert store.remove_item(folder["id"]) is True
    assert not (workspace / "files" / "资料").exists()
    assert (workspace / "files" / "资料-1" / "综述" / "a.pdf").is_file()


def test_folder_name_is_sanitized(store: ResearchStore):
    project = store.create_project("research", "项目")
    folder = store.create_folder(project["id"], "a/b|c?")
    assert folder["title"] == "b_c_"
    assert folder["path"] == "b_c_"


def test_path_escape_rejected(tmp_path: Path):
    workspace = tmp_path / "project"
    (workspace / "files").mkdir(parents=True)

    with pytest.raises(ValueError):
        resolve_in_files(workspace, "../outside.txt")

    relative, _ = save_bytes(workspace, "../../evil.pdf", b"x")
    assert relative == "evil.pdf"
    assert (workspace / "files" / "evil.pdf").is_file()
    assert not (workspace.parent / "evil.pdf").exists()


def test_sessions_index(store: ResearchStore):
    project = store.create_project("research")
    first = store.create_session(project["id"])
    assert first["title"] == "新会话"
    second = store.create_session(project["id"], "第二份会话")

    rows = store.list_sessions(project["id"])
    assert {row["id"] for row in rows} == {first["id"], second["id"]}

    # 固定时间，避免同秒创建的排序抖动。
    with store._db() as conn:
        conn.execute("UPDATE sessions SET updated_at = 100 WHERE id = ?", (first["id"],))
        conn.execute("UPDATE sessions SET updated_at = 200 WHERE id = ?", (second["id"],))

    updated = store.update_session(first["id"], title="改过的标题", status="running")
    assert updated is not None
    assert updated["title"] == "改过的标题"
    rows = store.list_sessions(project["id"])
    assert rows[0]["id"] == first["id"]
    assert store.get_project(project["id"])["session_count"] == 2
    assert store.get_project(project["id"])["running_count"] == 1


def test_session_events_dedupe_and_effects(store: ResearchStore):
    project = store.create_project("research")
    session = store.create_session(project["id"])
    session_id = session["id"]

    user = store.append_event(session_id, {"type": "message.user", "text": "你好"})
    assert user["seq"] == 1
    assert store.list_events(session_id) == [user]

    start = store.append_agent_event(
        session_id, 1, {"type": "turn.start", "turn": 1, "at": 1_700_000_000_001}
    )
    assert start["seq"] == 2
    assert store.get_session(session_id)["status"] == "running"

    # 同一 agent 事件重复到达（重连/第二个标签页）只保留一条。
    again = store.append_agent_event(
        session_id, 1, {"type": "turn.start", "turn": 1, "at": 1_700_000_000_001}
    )
    assert again["seq"] == start["seq"]
    assert store.last_agent_seq(session_id) == 1

    store.append_agent_event(
        session_id,
        2,
        {
            "type": "title.update",
            "title": "肝癌免疫治疗",
            "at": 1_700_000_000_002,
        },
    )
    assert store.get_session(session_id)["title"] == "肝癌免疫治疗"

    store.append_agent_event(
        session_id,
        3,
        {"type": "text.delta", "turn": 1, "text": "先看", "at": 1_700_000_000_003},
    )
    store.append_agent_event(
        session_id,
        4,
        {"type": "text.delta", "turn": 1, "text": "文献", "at": 1_700_000_000_004},
    )
    store.append_agent_event(
        session_id,
        5,
        {"type": "turn.end", "turn": 1, "reason": "done", "at": 1_700_000_000_005},
    )
    assert store.get_session(session_id)["status"] == "idle"

    texts = [row["text"] for row in store.list_events(session_id, after=2) if row["type"] == "text.delta"]
    assert texts == ["先看", "文献"]
    assert len(store.list_events(session_id)) == 6


def test_api_session_events_without_agent(
    client: TestClient, store: ResearchStore, monkeypatch
):
    monkeypatch.delenv("PAPEREAD_AGENT_PORT", raising=False)
    project = client.post("/api/research/projects", json={"kind": "research"}).json()
    session = client.post(
        f"/api/research/projects/{project['id']}/sessions", json={}
    ).json()

    for _ in range(2):
        with client.stream(
            "GET", f"/api/research/sessions/{session['id']}/events"
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())
        assert "agent.offline" in body
    # 离线提示只在连接内下发，重复刷新不应在库里堆积。
    assert [row["type"] for row in store.list_events(session["id"])] == []

    missing = client.get("/api/research/sessions/missing/events")
    assert missing.status_code == 404

    failed = client.post(
        f"/api/research/sessions/{session['id']}/messages", json={"text": "你好"}
    )
    assert failed.status_code == 503
    assert failed.json()["detail"] == "AGENT_UNAVAILABLE"

    events = client.get(
        f"/api/research/sessions/{session['id']}/events", params={"after": 0}
    )
    assert events.status_code == 200
    assert "你好" in events.text


def test_api_project_crud(client: TestClient):
    created = client.post(
        "/api/research/projects",
        json={"kind": "research", "name": "肝癌"},
    )
    assert created.status_code == 201
    project = created.json()
    assert project["kind"] == "research"
    assert project["name"] == "肝癌"

    listed = client.get("/api/research/projects", params={"kind": "research"}).json()
    assert [row["id"] for row in listed["items"]] == [project["id"]]
    assert client.get("/api/research/projects", params={"kind": "review"}).json()["items"] == []

    renamed = client.patch(
        f"/api/research/projects/{project['id']}", json={"name": "新名字"}
    ).json()
    assert renamed["name"] == "新名字"

    assert client.post("/api/research/projects", json={"kind": "bad"}).status_code == 400
    assert client.get("/api/research/projects/missing").status_code == 404


def test_api_files_and_sessions(client: TestClient, store: ResearchStore):
    project = client.post(
        "/api/research/projects", json={"kind": "review"}
    ).json()
    project_id = project["id"]

    folder = client.post(
        f"/api/research/projects/{project_id}/folders", json={"name": "文献"}
    )
    assert folder.status_code == 201
    folder_id = folder.json()["id"]
    assert folder.json()["kind"] == "folder"

    uploaded = client.post(
        f"/api/research/projects/{project_id}/files",
        files={"file": ("hcc.pdf", b"%PDF-1.4", "application/pdf")},
        data={"parentId": folder_id},
    )
    assert uploaded.status_code == 201
    file_item = uploaded.json()
    assert file_item["kind"] == "paper"
    assert file_item["parentId"] == folder_id
    assert file_item["path"] == "文献/hcc.pdf"

    files = client.get(f"/api/research/projects/{project_id}/files").json()["items"]
    assert {row["id"] for row in files} == {folder_id, file_item["id"]}

    content = client.get(
        f"/api/research/projects/{project_id}/files/{file_item['id']}/content"
    )
    assert content.status_code == 200
    assert content.content == b"%PDF-1.4"

    session = client.post(f"/api/research/projects/{project_id}/sessions", json={})
    assert session.status_code == 201
    assert session.json()["title"] == "新会话"
    sessions = client.get(
        f"/api/research/projects/{project_id}/sessions"
    ).json()["items"]
    assert [row["id"] for row in sessions] == [session.json()["id"]]

    archived = client.delete(f"/api/research/projects/{project_id}")
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert client.get(
        "/api/research/projects", params={"kind": "review"}
    ).json()["items"] == []


def test_api_item_rename_move_copy_delete(
    client: TestClient, store: ResearchStore
):
    project = client.post(
        "/api/research/projects", json={"kind": "research"}
    ).json()
    project_id = project["id"]

    folder = client.post(
        f"/api/research/projects/{project_id}/folders", json={"name": "文献"}
    ).json()
    sub = client.post(
        f"/api/research/projects/{project_id}/folders",
        json={"name": "综述", "parentId": folder["id"]},
    ).json()
    uploaded = client.post(
        f"/api/research/projects/{project_id}/files",
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        data={"parentId": folder["id"]},
    ).json()

    renamed = client.patch(
        f"/api/research/projects/{project_id}/items/{folder['id']}",
        json={"name": "资料"},
    )
    assert renamed.status_code == 200
    items = renamed.json()["items"]
    assert next(row for row in items if row["id"] == folder["id"])["path"] == "资料"
    assert next(row for row in items if row["id"] == uploaded["id"])["path"] == "资料/a.pdf"

    moved = client.patch(
        f"/api/research/projects/{project_id}/items/{uploaded['id']}",
        json={"parentId": sub["id"]},
    )
    assert moved.status_code == 200
    moved_items = moved.json()["items"]
    assert next(
        row for row in moved_items if row["id"] == uploaded["id"]
    )["path"] == "资料/综述/a.pdf"

    copied = client.post(
        f"/api/research/projects/{project_id}/items/{uploaded['id']}/copy",
        json={"parentId": folder["id"]},
    )
    assert copied.status_code == 201
    copied_items = copied.json()["items"]
    copy_row = next(
        row
        for row in copied_items
        if row["parentId"] == folder["id"] and row["kind"] == "paper"
    )
    assert copy_row["path"] == "资料/a.pdf"
    content = client.get(
        f"/api/research/projects/{project_id}/files/{copy_row['id']}/content"
    )
    assert content.content == b"%PDF-1.4"

    deleted = client.delete(
        f"/api/research/projects/{project_id}/items/{folder['id']}"
    )
    assert deleted.status_code == 200
    assert deleted.json()["items"] == []
    assert client.delete(
        f"/api/research/projects/{project_id}/items/missing"
    ).status_code == 404
