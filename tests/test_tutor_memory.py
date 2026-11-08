"""记忆系统的单元测试：不依赖真实模型，不依赖 FastAPI。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

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


@pytest.mark.parametrize("bad_version", [None, "1x"])
def test_invalid_version_is_backed_up_and_reset(store, bad_version):
    store.profile_path.write_text(
        json.dumps({"version": bad_version, "traits": {"depth": "x"}}),
        encoding="utf-8",
    )
    profile = store.profile()
    assert profile == {
        "version": memlib.VERSION,
        "updated_at": 0.0,
        "turn_count": 0,
        "traits": {},
    }
    backup = store.profile_path.with_name("profile.json.bak")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["version"] == bad_version


def test_older_version_is_accepted_and_normalized(store):
    store.profile_path.write_text(
        json.dumps({"version": 0, "traits": {"depth": "intro"}}), encoding="utf-8"
    )
    profile = store.profile()
    assert profile["version"] == memlib.VERSION
    assert profile["traits"] == {"depth": "intro"}
    assert not store.profile_path.with_name("profile.json.bak").exists()


def test_version_too_new_is_backed_up(store):
    store.concepts_path.write_text(
        json.dumps({"version": memlib.VERSION + 1, "concepts": [{"name": "x"}]}),
        encoding="utf-8",
    )
    assert store.concepts() == {"version": memlib.VERSION, "concepts": []}
    assert store.concepts_path.with_name("concepts.json.bak").exists()


def test_structurally_corrupt_chat_is_sanitized(store):
    store.paper_path("p1").write_text(
        json.dumps(
            {
                "version": 1,
                "paper_id": "p1",
                "messages": [
                    "x",
                    {"role": "user", "text": "无 id"},
                    {"id": "", "role": "user", "text": "空 id"},
                    {"id": "m1", "role": "system", "text": "坏角色"},
                    {"id": "m2", "role": "ai", "text": 42},
                ],
                "card": {"domains": "nope", "facts": "nope"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    data = store.chat("p1")
    assert [m["id"] for m in data["messages"]] == ["m2"]
    assert data["messages"][0]["text"] == "42"
    assert data["card"]["domains"] == []
    assert data["card"]["facts"] == []
    page = store.page("p1")
    assert [m["id"] for m in page["messages"]] == ["m2"]
    assert page["messages"][0]["at"] == 0.0
    assert store.recent_before("p1", "missing") == []


def test_chat_card_normalizes_domains_and_facts(store):
    store.paper_path("p1").write_text(
        json.dumps(
            {
                "version": 1,
                "paper_id": "p1",
                "messages": [],
                "card": {
                    "domains": ["a", 7, "", None, "b", "c", "d"],
                    "facts": [
                        {"kind": "conclusion", "text": "已确认"},
                        {"kind": "bogus", "text": "x"},
                        "not a fact",
                        {"kind": "open_question"},
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    card = store.chat("p1")["card"]
    assert card["domains"] == ["a", "7", "b", "c"]
    assert card["facts"] == [
        {"kind": "conclusion", "text": "已确认"},
        {"kind": "open_question", "text": ""},
    ]


def test_backup_collision_uses_unique_name(store):
    store.profile_path.write_text("{ bad", encoding="utf-8")
    store.profile_path.with_name("profile.json.bak").write_text("old", encoding="utf-8")
    assert store.profile()["version"] == memlib.VERSION
    backups = sorted(store.profile_path.parent.glob("profile.json*.bak"))
    assert len(backups) == 2
    assert (
        store.profile_path.with_name("profile.json.bak").read_text(encoding="utf-8")
        == "old"
    )


def test_backup_failure_does_not_escape_load(store, monkeypatch):
    store.profile_path.write_text("{ bad", encoding="utf-8")

    def deny(self, target):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "replace", deny)
    assert store.profile()["version"] == memlib.VERSION


def test_forget_removes_chat_backups_and_tmp(store):
    msg, _ = store.begin_question("p1", "q")
    store.finish_answer("p1", msg["id"], "a", [], False, 0.1)
    path = store.paper_path("p1")
    backup = path.with_name(path.name + ".bak")
    tmp = path.with_name(f"{path.name}.123.abcd.tmp")
    backup.write_text("{}", encoding="utf-8")
    tmp.write_text("{}", encoding="utf-8")

    other, _ = store.begin_question("p2", "other")
    store.finish_answer("p2", other["id"], "a", [], False, 0.1)

    store.forget("p1")
    assert not path.exists()
    assert not backup.exists()
    assert not tmp.exists()
    assert store.paper_path("p2").exists()


def test_memory_disabled_writes_no_files(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_REFLOW_MEMORY", "0")
    store = memlib.MemoryStore(tmp_path)
    msg, cached = store.begin_question("p1", "q", "c1")
    assert cached is None
    store.finish_answer("p1", msg["id"], "a", [], False, 0.1)
    store.seed("p1", title="T", domains=["d"])
    assert store.chat("p1")["messages"] == []
    assert store.page("p1") == {"messages": [], "hasMore": False}
    assert store.recent_before("p1", msg["id"]) == []
    assert [p for p in store.root.rglob("*") if p.is_file()] == []


def test_page_unknown_before_returns_empty(store):
    msg, _ = store.begin_question("p1", "q")
    store.finish_answer("p1", msg["id"], "a", [], False, 0.1)
    assert store.page("p1", before="missing") == {"messages": [], "hasMore": False}


def test_page_limit_is_clamped(store):
    for i in range(5):
        msg, _ = store.begin_question("p1", f"q{i}")
        store.finish_answer("p1", msg["id"], f"a{i}", [], False, 0.1)
    assert len(store.page("p1", limit=0)["messages"]) == 1
    assert len(store.page("p1", limit=-3)["messages"]) == 1
    wide = store.page("p1", limit=500)
    assert len(wide["messages"]) == 10
    assert wide["hasMore"] is False


def test_retry_same_client_id_before_answer_does_not_duplicate(store):
    first, cached = store.begin_question("p1", "q", "c1")
    again, cached_again = store.begin_question("p1", "q", "c1")
    assert cached is None
    assert cached_again is None
    assert again["id"] == first["id"]
    assert len(store.chat("p1")["messages"]) == 1


def test_finish_answer_is_idempotent(store):
    user, _ = store.begin_question("p1", "q")
    first = store.finish_answer("p1", user["id"], "a", [], False, 0.1)
    again = store.finish_answer("p1", user["id"], "a", [], False, 0.1)
    assert again["id"] == first["id"]
    assert [m["role"] for m in store.chat("p1")["messages"]] == ["user", "ai"]


def test_page_hides_internal_fields_and_round_trips_meta(store):
    user, _ = store.begin_question("p1", "q", "c1")
    store.finish_answer("p1", user["id"], "a", [{"page": 3}], True, 1.5)
    public = store.page("p1")["messages"]
    assert [m["role"] for m in public] == ["user", "ai"]
    assert "client_id" not in public[0]
    assert "answer_to" not in public[1]
    assert public[1]["sources"] == [{"page": 3}]
    assert public[1]["usedWeb"] is True
    assert public[1]["latency"] == 1.5


def test_get_store_returns_singleton_under_threads(tmp_path):
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait(timeout=5)
        results.append(memlib.get_store(tmp_path))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    assert all(store is results[0] for store in results)


def test_no_tmp_files_left_after_writes(store):
    for i in range(3):
        msg, _ = store.begin_question("p1", f"q{i}")
        store.finish_answer("p1", msg["id"], f"a{i}", [], False, 0.1)
    store.seed("p1", title="T", domains=["d"])
    assert list(store.papers.glob("*.tmp")) == []
    assert list(store.root.glob("*.tmp")) == []


def test_recent_before_n_zero_returns_empty(store):
    user, _ = store.begin_question("p1", "q")
    store.finish_answer("p1", user["id"], "a", [], False, 0.1)
    assert store.recent_before("p1", user["id"], 0) == []
    assert store.recent_before("p1", user["id"], -2) == []
