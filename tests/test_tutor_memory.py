"""记忆系统的单元测试：不依赖真实模型，不依赖 FastAPI。"""

from __future__ import annotations

import json

import pytest

from paper_reflow.tutor import memory as memlib


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_REFLOW_MEMORY", "1")
    return memlib.MemoryStore(tmp_path)


def test_begin_question_dedupes_same_client_id(store):
    first, cached = store.begin_question("p1", "什么是 BST？", "c1")
    assert cached is None
    store.finish_answer("p1", first["id"], "答案正文", [], False, 1.2)

    again, cached = store.begin_question("p1", "什么是 BST？", "c1")
    assert again["id"] == first["id"]
    assert cached is not None
    assert cached["text"] == "答案正文"
    assert len(store.chat("p1")["messages"]) == 2


def test_begin_question_creates_new_message_without_client_id(store):
    a, _ = store.begin_question("p1", "第一个问题")
    b, _ = store.begin_question("p1", "第二个问题")
    assert a["id"] != b["id"]
    assert len(store.chat("p1")["messages"]) == 2


def test_recent_before_excludes_current_message(store):
    ids = []
    for i in range(4):
        msg, _ = store.begin_question("p1", f"q{i}")
        store.finish_answer("p1", msg["id"], f"a{i}", [], False, 0.1)
        ids.append(msg["id"])

    current, _ = store.begin_question("p1", "q4")
    recent = store.recent_before("p1", current["id"], 6)
    assert [m["text"] for m in recent] == ["q0", "a0", "q1", "a1", "q2", "a2", "q3", "a3"][-6:]
    assert all(m["id"] != current["id"] for m in recent)


def test_page_returns_latest_and_has_more(store):
    for i in range(5):
        msg, _ = store.begin_question("p1", f"q{i}")
        store.finish_answer("p1", msg["id"], f"a{i}", [], False, 0.1)

    page = store.page("p1", limit=3)
    assert [m["text"] for m in page["messages"]] == ["a3", "q4", "a4"]
    assert page["hasMore"] is True
    assert all(m["role"] in ("user", "ai") for m in page["messages"])


def test_corrupt_profile_is_backed_up(store):
    store.profile_path.write_text("{ not json", encoding="utf-8")
    profile = store.profile()
    assert profile["version"] == 1
    assert store.profile_path.with_name("profile.json.bak").exists()


def test_seed_fills_title_and_domains_once(store):
    store.seed("p1", title="Paper A", domains=["图学习", "链接预测"])
    store.seed("p1", title="Paper B", domains=["生物"])
    card = store.chat("p1")["card"]
    assert card["title"] == "Paper A"
    assert card["domains"] == ["图学习", "链接预测", "生物"]


def test_saved_files_are_valid_json(store):
    msg, _ = store.begin_question("p1", "q")
    store.finish_answer("p1", msg["id"], "a", [], False, 0.1)
    path = store.paper_path("p1")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["paper_id"] == "p1"
    assert len(data["messages"]) == 2
