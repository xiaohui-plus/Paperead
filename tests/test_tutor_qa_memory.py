"""后台记忆写手测试：用 fake backend，不联网。"""

from __future__ import annotations

import asyncio
import json

import pytest

from paper_reflow.llm import set_backend
from paper_reflow.tutor import memory as memlib


class FakeBackend:
    def __init__(self, reply: str = "", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.seen: list[list[dict]] = []
        self.kwargs: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.seen.append(list(messages))
        self.kwargs.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture(autouse=True)
def reset_backend():
    yield
    set_backend(None)


def test_write_memory_applies_writer_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_REFLOW_MEMORY", "1")
    store = memlib.MemoryStore(tmp_path)
    reply = json.dumps(
        {
            "traits": [
                {
                    "key": "answer_style",
                    "value": "先结论后分点",
                    "confidence": 0.7,
                    "evidence": "要求直接给结论",
                }
            ],
            "concepts": [
                {"name": "消息传递", "state": "learning", "evidence": "在追问聚合方式"}
            ],
            "summary": "用户已经理解消息传递的基本直觉，正在追问 BST 如何编码成对结构，尚未确认与 SEAL 的对比是否公平。",
            "facts": [{"kind": "user_stance", "text": "用户认为本文的 ablation 不够"}],
        },
        ensure_ascii=False,
    )
    fake = FakeBackend(reply)
    set_backend(fake)
    asyncio.run(
        memlib.write_memory(
            store,
            "p1",
            "BST 怎么编码成对结构",
            "答案",
            "BST 怎么编码成对结构？",
            ["消息传递"],
        )
    )

    assert store.profile()["traits"]["answer_style"]["value"] == "先结论后分点"
    assert store.concepts()["concepts"][0]["state"] == "learning"
    assert "BST" in store.chat("p1")["card"]["summary"]
    assert store.chat("p1")["card"]["facts"][0]["kind"] == "user_stance"

    assert len(fake.seen) == 1
    assert fake.kwargs[0]["temperature"] == 0.1
    assert fake.kwargs[0]["max_tokens"] == 700
    payload = json.loads(fake.seen[0][1]["content"])
    assert payload["本轮问题"] == "BST 怎么编码成对结构？"
    assert payload["本轮回答"] == "答案"
    assert payload["涉及概念"] == ["消息传递"]
    assert set(payload) >= {
        "本轮问题",
        "本轮回答",
        "现有小结",
        "现有事实",
        "现有画像",
        "涉及概念",
    }


def test_write_memory_falls_back_when_model_returns_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_REFLOW_MEMORY", "1")
    store = memlib.MemoryStore(tmp_path)
    set_backend(FakeBackend("这不是 JSON"))
    asyncio.run(
        memlib.write_memory(store, "p1", "还是不懂", "答案", "还是不懂", ["消息传递"])
    )

    concept = store.concepts()["concepts"][0]
    assert concept["state"] == "confused"


def test_write_memory_survives_backend_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_REFLOW_MEMORY", "1")
    store = memlib.MemoryStore(tmp_path)
    set_backend(FakeBackend(error=RuntimeError("模型挂了")))

    asyncio.run(
        memlib.write_memory(store, "p1", "还是不懂", "答案", "还是不懂", ["消息传递"])
    )

    concept = store.concepts()["concepts"][0]
    assert concept["state"] == "confused"


def test_write_memory_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_REFLOW_MEMORY", "0")
    store = memlib.MemoryStore(tmp_path)
    fake = FakeBackend("{}")
    set_backend(fake)

    asyncio.run(
        memlib.write_memory(store, "p1", "问题", "答案", "问题", ["消息传递"])
    )

    assert fake.seen == []
    assert not store.profile_path.exists()
    assert not store.concepts_path.exists()
    assert not store.paper_path("p1").exists()
    assert list(store.papers.iterdir()) == []
