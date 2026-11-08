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


def test_overflow_version_is_backed_up(store):
    store.profile_path.write_text('{"version": 1e400, "traits": {}}', encoding="utf-8")
    assert store.profile()["version"] == memlib.VERSION
    backup = store.profile_path.with_name("profile.json.bak")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8"))["version"] == float("inf")


def test_older_version_is_accepted_and_normalized(store):
    store.profile_path.write_text(
        json.dumps(
            {
                "version": 0,
                "traits": {
                    "depth": {
                        "value": "需要类比",
                        "confidence": 0.6,
                        "evidence": ["x"],
                        "updated_at": 0,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profile = store.profile()
    assert profile["version"] == memlib.VERSION
    assert profile["traits"] == {
        "depth": {
            "value": "需要类比",
            "confidence": 0.6,
            "evidence": ["x"],
            "updated_at": 0.0,
        }
    }
    assert isinstance(profile["traits"]["depth"]["updated_at"], float)
    assert not store.profile_path.with_name("profile.json.bak").exists()


def test_profile_normalizes_and_drops_invalid_traits(store):
    store.profile_path.write_text(
        json.dumps(
            {
                "version": memlib.VERSION,
                "traits": {
                    "depth": {
                        "value": "  需要类比  ",
                        "confidence": 1.7,
                        "evidence": ["a", "b", "c", "d"],
                        "updated_at": "3.5",
                    },
                    "goal": "想用于自己的数据",
                    "favorite_color": {
                        "value": "蓝色",
                        "confidence": 0.6,
                        "evidence": ["x"],
                        "updated_at": 0,
                    },
                    "language": {"value": "中", "confidence": 0.6, "evidence": ["x"]},
                    "answer_style": {"value": "先给结论", "evidence": "not a list"},
                    "math_comfort": ["不是字典"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    traits = store.profile()["traits"]
    assert set(traits) == {"depth", "answer_style"}
    assert traits["depth"] == {
        "value": "需要类比",
        "confidence": 0.9,
        "evidence": ["b", "c", "d"],
        "updated_at": 3.5,
    }
    assert traits["answer_style"] == {
        "value": "先给结论",
        "confidence": 0.5,
        "evidence": [],
        "updated_at": 0.0,
    }


@pytest.mark.parametrize("turn_count", ["x", 1e400])
def test_profile_normalizes_turn_count_and_updated_at(store, turn_count):
    store.profile_path.write_text(
        json.dumps(
            {
                "version": memlib.VERSION,
                "turn_count": turn_count,
                "updated_at": "bad",
                "traits": {},
            }
        ),
        encoding="utf-8",
    )
    profile = store.profile()
    assert profile["turn_count"] == 0
    assert isinstance(profile["turn_count"], int)
    assert profile["updated_at"] == 0.0
    assert isinstance(profile["updated_at"], float)


def test_version_too_new_is_backed_up(store):
    store.concepts_path.write_text(
        json.dumps({"version": memlib.VERSION + 1, "concepts": [{"name": "x"}]}),
        encoding="utf-8",
    )
    assert store.concepts() == {"version": memlib.VERSION, "concepts": []}
    assert store.concepts_path.with_name("concepts.json.bak").exists()


def test_concepts_drop_invalid_entries_and_normalize_key(store):
    store.concepts_path.write_text(
        json.dumps(
            {
                "version": memlib.VERSION,
                "concepts": [
                    {
                        "name": "Message Passing",
                        "state": "learning",
                        "confidence": 0.5,
                        "aliases": ["消息传递", 3, ""],
                        "papers": ["p1", 7],
                        "evidence": [{"paper_id": "p1", "quote": "x"}, "bad"],
                        "times_asked": "3",
                        "last_seen": "1.5",
                    },
                    {"key": "消息传递", "state": "understood", "confidence": 0.8},
                    "not a concept",
                    {"name": "没有状态", "confidence": 0.5},
                    {"name": "状态非法", "state": "bogus", "confidence": 0.5},
                    {"name": "置信度非法", "state": "learning", "confidence": "high"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    items = store.concepts()["concepts"]
    assert [c["name"] for c in items] == ["Message Passing", "消息传递"]
    first = items[0]
    assert first["key"] == "message passing"
    assert first["aliases"] == ["消息传递"]
    assert first["papers"] == ["p1"]
    assert first["evidence"] == [{"paper_id": "p1", "quote": "x"}]
    assert first["times_asked"] == 3
    assert first["last_seen"] == 1.5
    assert items[1]["key"] == "消息传递"
    assert items[1]["confidence"] == 0.8


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
                    "domains": ["a", 7, "", None, "b", "a", "c", "d"],
                    "facts": [
                        {"kind": "conclusion", "text": "已确认"},
                        {"kind": "conclusion", "text": "  已确认  "},
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
    assert card["domains"] == ["a", "b", "c", "d"]
    assert card["facts"] == [{"kind": "conclusion", "text": "已确认"}]


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
    assert store.history("p1") == {"turns": [], "hasMore": False}
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


def test_history_pairs_turns_and_pages(store):
    sources = [{"type": "paper", "label": "P1"}]
    for i in range(4):
        msg, _ = store.begin_question("p1", f"q{i}")
        store.finish_answer("p1", msg["id"], f"a{i}", sources, i == 1, 0.5 + i)

    latest = store.history("p1", limit=2)
    assert [t["question"] for t in latest["turns"]] == ["q2", "q3"]
    assert [t["answer"] for t in latest["turns"]] == ["a2", "a3"]
    assert latest["turns"][0]["sources"] == sources
    assert latest["turns"][0]["usedWeb"] is False
    assert latest["turns"][0]["latency"] == 2.5
    assert latest["turns"][1]["usedWeb"] is False
    assert latest["hasMore"] is True

    older = store.history("p1", before=latest["turns"][0]["id"], limit=2)
    assert [t["question"] for t in older["turns"]] == ["q0", "q1"]
    assert older["turns"][1]["usedWeb"] is True
    assert older["hasMore"] is False

    assert len(store.history("p1", limit=0)["turns"]) == 1
    assert len(store.history("p1", limit=500)["turns"]) == 4


def test_history_keeps_unanswered_and_skips_orphan_answer(store):
    msg, _ = store.begin_question("p1", "还在等回答")
    data = store.chat("p1")
    data["messages"].append(
        {
            "id": "m_orphan",
            "role": "ai",
            "text": "没有对应提问",
            "answer_to": "m_missing",
            "sources": [],
            "used_web": False,
            "latency": 0.3,
            "at": 0.0,
        }
    )
    store._save_chat("p1", data)

    turns = store.history("p1")["turns"]
    assert turns == [
        {
            "id": msg["id"],
            "question": "还在等回答",
            "answer": "",
            "sources": [],
            "usedWeb": False,
            "latency": 0.0,
            "at": msg["at"],
        }
    ]



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


def test_concurrent_begin_question_keeps_all_messages(store):
    count = 16
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        barrier.wait(timeout=5)
        store.begin_question("p1", f"q{index}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    data = json.loads(store.paper_path("p1").read_text(encoding="utf-8"))
    assert len(data["messages"]) == count
    assert len(store.chat("p1")["messages"]) == count
    assert list(store.papers.glob("*.tmp")) == []


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


def test_merge_trait_updates_confidence_conservatively(store):
    store.apply_patch(
        "p1",
        {"traits": [{"key": "depth", "value": "需要类比", "confidence": 0.6, "evidence": "要求类比"}]},
    )
    first = store.profile()["traits"]["depth"]
    assert first["value"] == "需要类比"
    assert first["confidence"] == pytest.approx(0.6, abs=0.01)

    store.apply_patch(
        "p1",
        {"traits": [{"key": "depth", "value": "需要类比和前置", "confidence": 0.9, "evidence": "再次要求"}]},
    )
    second = store.profile()["traits"]["depth"]
    # 0.6*0.6 + 0.4*0.9 = 0.72，单轮变化不超过 ±0.2
    assert second["confidence"] == pytest.approx(0.72, abs=0.01)
    assert len(second["evidence"]) == 2


def test_merge_rejects_unknown_trait_and_empty_evidence(store):
    store.apply_patch("p1", {"traits": [{"key": "favorite_color", "value": "blue", "confidence": 0.9, "evidence": "x"}]})
    store.apply_patch("p1", {"traits": [{"key": "depth", "value": "需要类比", "confidence": 0.9, "evidence": ""}]})
    assert store.profile()["traits"] == {}


def test_concept_state_can_go_up_but_not_down_without_explicit(store):
    store.apply_patch("p1", {"concepts": [{"name": "消息传递", "state": "confused", "evidence": "没看懂", "explicit": True}]})
    store.apply_patch("p1", {"concepts": [{"name": "消息传递", "state": "understood", "evidence": "懂了"}]})
    assert store.concepts()["concepts"][0]["state"] == "understood"

    store.apply_patch("p1", {"concepts": [{"name": "消息传递", "state": "confused", "evidence": "又糊涂了"}]})
    assert store.concepts()["concepts"][0]["state"] == "understood"

    store.apply_patch("p1", {"concepts": [{"name": "消息传递", "state": "confused", "evidence": "还是不懂", "explicit": True}]})
    assert store.concepts()["concepts"][0]["state"] == "confused"


def test_concept_alias_merges_same_entry(store):
    store.apply_patch(
        "p1",
        {
            "concepts": [
                {
                    "name": "Message Passing",
                    "aliases": ["消息传递"],
                    "state": "learning",
                    "evidence": "a",
                }
            ]
        },
    )
    store.apply_patch("p1", {"concepts": [{"name": "消息传递", "state": "understood", "evidence": "b"}]})
    items = store.concepts()["concepts"]
    assert len(items) == 1
    assert items[0]["state"] == "understood"


def test_concept_explicit_requires_literal_true(store):
    store.apply_patch("p1", {"concepts": [{"name": "消息传递", "state": "understood", "evidence": "懂了"}]})
    for flag in ("true", 1, "false"):
        store.apply_patch(
            "p1",
            {
                "concepts": [
                    {
                        "name": "消息传递",
                        "state": "confused",
                        "evidence": "又糊涂了",
                        "explicit": flag,
                    }
                ]
            },
        )
        assert store.concepts()["concepts"][0]["state"] == "understood"


def test_concept_rejected_downgrade_does_not_add_alias(store):
    store.apply_patch("p1", {"concepts": [{"name": "消息传递", "state": "understood", "evidence": "懂了"}]})
    store.apply_patch(
        "p1",
        {"concepts": [{"name": "消息传递算法", "state": "confused", "evidence": "又糊涂了"}]},
    )
    concept = store.concepts()["concepts"][0]
    assert concept["state"] == "understood"
    assert "消息传递算法" not in concept["aliases"]


def test_merge_rejects_non_string_patch_fields(store):
    store.apply_patch(
        "p1",
        {
            "traits": [
                {"key": "depth", "value": None, "confidence": 0.6, "evidence": "x"},
                {"key": 123, "value": "需要类比", "confidence": 0.6, "evidence": "x"},
                {
                    "key": "goal",
                    "value": "用于单细胞数据",
                    "confidence": 0.6,
                    "evidence": {"quote": "x"},
                },
            ],
            "concepts": [
                {"name": None, "state": "learning", "evidence": "x"},
                {"name": "消息传递", "state": True, "evidence": "x"},
                {"name": "！！", "state": "learning", "evidence": "x"},
            ],
            "facts": [
                {"kind": None, "text": "足够长的文本内容"},
                {"kind": "conclusion", "text": {"quote": "x"}},
            ],
            "summary": {"text": "用户已经理解了消息传递"},
            "card": {"one_liner": 123, "domains": [{"bad": 1}, None, 7]},
        },
    )
    assert store.profile()["traits"] == {}
    assert store.concepts()["concepts"] == []
    assert store.chat("p1")["card"]["facts"] == []
    assert store.chat("p1")["card"]["summary"] == ""
    assert store.chat("p1")["card"]["one_liner"] == ""
    assert store.chat("p1")["card"]["domains"] == []


def test_merge_card_summary_and_summarized_through(store):
    user, _ = store.begin_question("p1", "q")
    ai = store.finish_answer("p1", user["id"], "a", [], False, 0.1)

    store.apply_patch("p1", {"summary": "太短"})
    assert store.chat("p1")["card"]["summary"] == ""

    good = "用户主要关心公式含义与 ablation 是否充分，已确认 BST 的时间复杂度，仍不清楚与 SEAL 的参数量是否一致。"
    store.apply_patch("p1", {"summary": good})
    card = store.chat("p1")["card"]
    assert card["summary"] == good
    assert card["summarized_through"] == ai["id"]

    store.begin_question("p1", "后续问题")
    store.apply_patch("p1", {"summary": good + "补充"})
    card = store.chat("p1")["card"]
    assert card["summary"] == good + "补充"
    assert card["summarized_through"] == ai["id"]


def test_merge_card_dedupes_facts_by_text(store):
    text = "用户认为本文 ablation 缺少 width 维度"
    store.apply_patch("p1", {"facts": [{"kind": "user_stance", "text": text}]})
    store.apply_patch("p1", {"facts": [{"kind": "user_stance", "text": text + "。"}]})
    facts = store.chat("p1")["card"]["facts"]
    assert len(facts) == 1
    assert facts[0]["text"] == text


def test_facts_eviction_protects_user_stance(store):
    stance = "用户认为本文的消融实验缺少 width 维度"
    questions = [
        "用户还没搞懂拓扑采样",
        "用户还没搞懂消息传递",
        "用户还没搞懂节点嵌入",
        "用户还没搞懂注意力机制",
        "用户还没搞懂损失函数",
        "用户还没搞懂梯度下降",
        "用户还没搞懂卷积核",
        "用户还没搞懂池化层",
        "用户还没搞懂残差连接",
        "用户还没搞懂归一化",
        "用户还没搞懂对抗训练",
    ]
    store.apply_patch("p1", {"facts": [{"kind": "user_stance", "text": stance}]})
    for text in questions:
        store.apply_patch("p1", {"facts": [{"kind": "open_question", "text": text}]})

    facts = store.chat("p1")["card"]["facts"]
    assert len(facts) == 12
    assert facts[0]["kind"] == "user_stance"

    store.apply_patch("p1", {"facts": [{"kind": "open_question", "text": "用户还没搞懂对比学习"}]})
    facts = store.chat("p1")["card"]["facts"]
    assert len(facts) == 12
    assert facts[0]["kind"] == "user_stance"


def test_merge_card_one_liner_first_only(store):
    store.apply_patch("p1", {"card": {"one_liner": "  用子图采样做链接预测  "}})
    assert store.chat("p1")["card"]["one_liner"] == "用子图采样做链接预测"
    store.apply_patch("p1", {"card": {"one_liner": "第二版一句话"}})
    assert store.chat("p1")["card"]["one_liner"] == "用子图采样做链接预测"

    store.apply_patch("p2", {"card": {"one_liner": "长" * 61}})
    assert store.chat("p2")["card"]["one_liner"] == ""


def test_merge_card_domains_union_capped_and_str_only(store):
    store.apply_patch("p1", {"card": {"domains": ["图学习", None, 7, "图学习", " 链接预测 "]}})
    assert store.chat("p1")["card"]["domains"] == ["图学习", "链接预测"]

    store.apply_patch(
        "p1", {"card": {"domains": ["图神经网络", {"bad": 1}, ["x"], "知识图谱"]}}
    )
    assert store.chat("p1")["card"]["domains"] == [
        "图学习",
        "链接预测",
        "图神经网络",
        "知识图谱",
    ]

    store.apply_patch("p1", {"card": {"domains": ["第五个领域"]}})
    assert store.chat("p1")["card"]["domains"] == [
        "图学习",
        "链接预测",
        "图神经网络",
        "知识图谱",
    ]


def test_merge_summary_rejects_too_short(store):
    store.apply_patch("p1", {"summary": "太短"})
    assert store.chat("p1")["card"]["summary"] == ""
    good = "用户主要关心公式含义与 ablation 是否充分，已确认 BST 的时间复杂度，仍不清楚与 SEAL 的参数量是否一致。"
    store.apply_patch("p1", {"summary": good})
    assert store.chat("p1")["card"]["summary"] == good


def test_facts_dedupe_and_cap(store):
    text = "用户认为本文 ablation 缺少 width 维度"
    store.apply_patch("p1", {"facts": [{"kind": "user_stance", "text": text}]})
    store.apply_patch("p1", {"facts": [{"kind": "user_stance", "text": text + "。"}]})
    assert len(store.chat("p1")["card"]["facts"]) == 1

    for i in range(15):
        store.apply_patch(
            "p1",
            {"facts": [{"kind": "conclusion", "text": f"这是第 {i} 条足够长的结论内容"}]},
        )
    assert len(store.chat("p1")["card"]["facts"]) <= 12


def test_fallback_rules_mark_concept_confused(store):
    patch = store.fallback_patch("还是不懂，消息传递到底怎么传", ["消息传递"])
    store.apply_patch("p1", patch)
    concept = store.concepts()["concepts"][0]
    assert concept["state"] == "confused"
    assert any(f["kind"] == "open_question" for f in store.chat("p1")["card"]["facts"])


@pytest.mark.parametrize(
    ("question", "expected_state", "expect_open_question", "expect_stance"),
    [
        ("我不懂了", "confused", True, False),
        ("不懂了", "confused", True, False),
        ("不明白", "confused", True, False),
        ("还是不明白", "confused", True, False),
        ("没听懂", "confused", True, False),
        ("没理解", "confused", True, False),
        ("没听懂了", "confused", True, False),
        ("懂了", "understood", False, False),
        ("不对", None, False, True),
        ("我懂了，不对", "understood", False, True),
        ("不对，我还是不懂", "confused", True, True),
    ],
)
def test_fallback_classification(
    store, question, expected_state, expect_open_question, expect_stance
):
    patch = store.fallback_patch(question, ["消息传递"])
    assert [c["state"] for c in patch["concepts"]] == (
        [expected_state] if expected_state else []
    )
    assert all(c["explicit"] is True for c in patch["concepts"])
    assert (
        any(f["kind"] == "open_question" for f in patch["facts"])
        is expect_open_question
    )
    assert (
        any(f["kind"] == "user_stance" for f in patch["facts"]) is expect_stance
    )


def test_clean_facts_over_cap_keeps_user_stance(store):
    facts = [{"kind": "user_stance", "text": "用户认为本文的消融实验缺少 width 维度"}]
    facts += [
        {"kind": "open_question", "text": f"用户还没搞懂第 {i} 个概念"}
        for i in range(12)
    ]
    facts.append({"kind": "conclusion", "text": "已确认 BST 的时间复杂度"})
    store.paper_path("p1").write_text(
        json.dumps(
            {
                "version": 1,
                "paper_id": "p1",
                "messages": [],
                "card": {"facts": facts},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cleaned = store.chat("p1")["card"]["facts"]
    assert len(cleaned) == memlib.MAX_FACTS
    assert cleaned[0]["kind"] == "user_stance"
    assert any(f["kind"] == "user_stance" for f in cleaned)


def test_chat_card_normalizes_scalar_fields(store):
    store.paper_path("p1").write_text(
        json.dumps(
            {
                "version": 1,
                "paper_id": "p1",
                "messages": [],
                "card": {
                    "title": [],
                    "summary": None,
                    "one_liner": "  " + "长" * 80 + "  ",
                    "summarized_through": 42,
                    "updated_at": "bad",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    card = store.chat("p1")["card"]
    assert card["title"] == ""
    assert card["summary"] == ""
    assert card["one_liner"] == "长" * 60
    assert card["summarized_through"] == ""
    assert card["updated_at"] == 0.0
    assert isinstance(card["updated_at"], float)

    store.paper_path("p2").write_text(
        json.dumps(
            {
                "version": 1,
                "paper_id": "p2",
                "messages": [],
                "card": {
                    "title": "  " + "题" * 250,
                    "summary": "  " + "结" * 450,
                    "one_liner": "一句话",
                    "summarized_through": "m_1",
                    "updated_at": "3.5",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    card = store.chat("p2")["card"]
    assert card["title"] == "题" * 200
    assert card["summary"] == "结" * 400
    assert card["one_liner"] == "一句话"
    assert card["summarized_through"] == "m_1"
    assert card["updated_at"] == 3.5


def test_seed_normalizes_non_string_domains(store):
    store.seed("p1", title={"a": 1}, domains=[{"x": 1}, "图学习", 7])
    card = store.chat("p1")["card"]
    assert card["title"] == ""
    assert card["domains"] == ["图学习"]


def test_clean_domains_truncates_each_domain(store):
    store.seed("p1", domains=["领" * 25])
    assert store.chat("p1")["card"]["domains"] == ["领" * 20]


def test_brief_includes_profile_concepts_and_card(store):
    store.apply_patch(
        "p1",
        {
            "traits": [{"key": "depth", "value": "需要类比", "confidence": 0.8, "evidence": "要求类比"}],
            "concepts": [{"name": "消息传递", "state": "understood", "evidence": "懂了", "explicit": True}],
            "summary": "用户已经理解了消息传递，正在关心 BST 的时间复杂度，还没有搞懂与 SEAL 的对比是否公平。",
        },
    )
    brief = store.build_brief("p1", "消息传递在 BST 里怎么用？")
    assert "理解深度：需要类比" in brief
    assert "消息传递：已懂" in brief
    assert "BST 的时间复杂度" in brief


def test_brief_omits_stale_low_confidence_trait(store):
    old = memlib._now() - 400 * 86400
    profile = store.profile()
    profile["traits"]["goal"] = {
        "value": "判断能否用于自己的数据",
        "confidence": 0.8,
        "evidence": ["x"],
        "updated_at": old,
    }
    profile["updated_at"] = old
    memlib._atomic_write(store.profile_path, profile)
    brief = store.build_brief("p1", "这篇能用在单细胞数据上吗")
    assert "判断能否用于自己的数据" not in brief


def test_brief_retrieves_old_turn(store):
    for text in [
        "这篇的 baseline 是 SEAL",
        "BST 用二元结构编码成对关系",
        "时间复杂度是线性的",
        "数据集用了 DREAM5",
        "消融实验缺少 width 维度",
    ]:
        msg, _ = store.begin_question("p1", text)
        store.finish_answer("p1", msg["id"], f"关于{text}的回答", [], False, 0.1)
    msg, _ = store.begin_question("p1", "当前问题")
    for i in range(3):
        extra, _ = store.begin_question("p1", f"填充{i}")
        store.finish_answer("p1", extra["id"], f"填充答案{i}", [], False, 0.1)

    brief = store.build_brief("p1", "SEAL 的 baseline 公平吗", exclude_message_id=msg["id"])
    assert "SEAL" in brief


def test_brief_flags_repeated_question(store):
    msg, _ = store.begin_question("p1", "这篇论文的核心创新点是什么")
    store.finish_answer("p1", msg["id"], "核心创新是 BST", [], False, 0.1)
    current, _ = store.begin_question("p1", "这篇论文的核心创新点是什么？")
    brief = store.build_brief("p1", "这篇论文的核心创新点是什么", exclude_message_id=current["id"])
    assert "此前问过" in brief


def test_brief_cross_paper_card(store):
    store.seed("p2", title="SEAL 论文", domains=["链接预测"])
    store.apply_patch(
        "p2",
        {"card": {"one_liner": "用子图采样做链接预测"}, "facts": [{"kind": "user_stance", "text": "用户认为 SEAL 的训练开销太大"}]},
    )
    brief = store.build_brief("p1", "SEAL 的训练开销和这篇比呢")
    assert "SEAL 论文" in brief
    assert "训练开销太大" in brief


def test_brief_empty_memory_returns_empty(store):
    assert store.build_brief("p1", "任意问题") == ""


def test_brief_disabled_returns_empty(store, monkeypatch):
    store.apply_patch(
        "p1",
        {"summary": "用户已经理解了消息传递，正在关心 BST 的时间复杂度，还没有搞懂与 SEAL 的对比是否公平。"},
    )
    monkeypatch.setenv("PAPER_REFLOW_MEMORY", "0")
    assert store.build_brief("p1", "消息传递怎么用") == ""


def test_brief_tolerates_malformed_other_paper(store):
    store.paper_path("p2").write_text(
        json.dumps(
            {
                "version": 1,
                "paper_id": "p2",
                "messages": [],
                "card": {
                    "title": ["bad"],
                    "domains": [{"x": 1}, None],
                    "summary": 42,
                    "one_liner": "SEAL 一句话",
                    "facts": "not-a-list",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    brief = store.build_brief("p1", "SEAL 一句话")
    assert isinstance(brief, str)
    assert "《未命名》：SEAL 一句话" in brief


def test_brief_stays_within_budget(store):
    for key in memlib.TRAIT_KEYS:
        store.apply_patch(
            "p1",
            {"traits": [{"key": key, "value": "特" * 40, "confidence": 0.9, "evidence": "x"}]},
        )

    concept_names = ["概" * 39 + str(i) for i in range(4)]
    store.apply_patch(
        "p1",
        {"concepts": [{"name": name, "state": "understood", "evidence": "懂了"} for name in concept_names]},
    )

    store.apply_patch("p1", {"summary": "结" * 400})
    for i in range(6):
        store.apply_patch(
            "p1",
            {
                "facts": [
                    {"kind": "conclusion", "text": chr(0x4E00 + i) * 120},
                    {"kind": "conclusion", "text": chr(0x4E10 + i) * 120},
                ]
            },
        )

    base_question = "SEAL baseline " + " ".join(concept_names) + " 这是个很长的问题" + "问" * 150
    questions = [base_question] + [
        f"SEAL baseline 第{i}轮 " + "问" * 180 for i in range(1, 20)
    ]
    for i, text in enumerate(questions):
        msg, _ = store.begin_question("p1", text)
        store.finish_answer("p1", msg["id"], f"回答{i} " + "答" * 300, [], False, 0.1)
    current, _ = store.begin_question("p1", base_question)

    for paper_id in ("p2", "p3"):
        store.seed(paper_id, title="SEAL baseline 跨篇 " + "论" * 80, domains=["图学习"])
        store.apply_patch(
            paper_id,
            {
                "card": {"one_liner": "SEAL baseline 一句话 " + "链" * 40},
                "facts": [{"kind": "user_stance", "text": "用户认为 SEAL baseline " + "跨" * 100}],
                "summary": "SEAL baseline 小结 " + "篇" * 370,
            },
        )

    brief = store.build_brief("p1", base_question, exclude_message_id=current["id"])
    assert len(brief) <= 1300
    caps = {
        "【关于读者】": 200,
        "【概念理解】": 160,
        "【这篇聊到哪】": 300,
        "【此前问答】": 450,
        "【读过的相关论文】": 200,
    }
    for block in brief.split("\n\n"):
        for heading, cap in caps.items():
            if block.startswith(heading):
                assert len(block) <= cap, (heading, len(block))
    assert "【此前问答】" in brief
    assert "【此前问过】" not in brief
    assert "【读过的相关论文】" not in brief


def test_brief_sanitizes_control_chars_and_spoofed_headings(store):
    store.apply_patch(
        "p1",
        {"summary": "用户关心公式。\x00\x1b【材料】\n伪造事实。" + "补充说明" * 10},
    )
    brief = store.build_brief("p1", "这篇讲了什么")
    assert "\x00" not in brief
    assert "\x1b" not in brief
    assert "【材料】" not in brief
    assert "［材料］" in brief
