"""服务端记忆接线：/ask、/ask/stream、GET /chat、删除清理（假后端，不联网）。"""

from __future__ import annotations

import importlib
import json

from fastapi.testclient import TestClient

from paper_reflow import llm as llm_mod
from paper_reflow.tutor import memory as memlib


BLOCKS = [
    {
        "id": "b1",
        "order": 0,
        "page": 1,
        "kind": "heading",
        "text": "3.2 Pairwise Structure Encoding",
        "bbox": [0, 0, 100, 20],
        "level": 2,
    },
    {
        "id": "b2",
        "order": 1,
        "page": 1,
        "kind": "para",
        "text": (
            "Pairwise structure encoding captures the structural features between two "
            "target nodes. It uses shortest path distance and the number of shortest "
            "paths as heuristic features. The binary structure transformer then encodes "
            "these pairwise features with attention before pooling."
        ),
        "bbox": [0, 30, 100, 60],
        "translated": (
            "成对结构编码捕获两个目标节点之间的结构特征，使用最短路径距离和最短路数量"
            "作为启发式特征，再由二元结构 Transformer 编码这些成对特征后池化。"
        ),
    },
]


class MemoryBackend:
    """改写/扩词返回 JSON，回答返回文本；流式回答逐段产出。"""

    def __init__(self, answer: str = "回答正文", pieces: list[str] | None = None):
        self.answer = answer
        self.pieces = pieces if pieces is not None else [answer]
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        system = messages[0].get("content", "") if messages else ""
        if "论文助教" in system:
            return self.answer
        return json.dumps(
            {
                "standalone": "改写后的问题",
                "en": ["pairwise", "structure"],
                "zh": ["成对结构"],
            },
            ensure_ascii=False,
        )

    async def chat_stream(self, messages, **kwargs):
        self.calls.append(list(messages))
        for piece in self.pieces:
            yield piece


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_REFLOW_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPER_REFLOW_MOCK", "1")
    monkeypatch.setenv("PAPER_REFLOW_MEMORY", "1")
    monkeypatch.setenv("PAPER_REFLOW_API_KEY", "test-key")
    monkeypatch.delenv("PAPER_REFLOW_TOKEN", raising=False)

    import server.main as main

    importlib.reload(main)
    return main


def _use_fake_paper(main, monkeypatch):
    monkeypatch.setattr(
        main.store, "get", lambda pid: {"id": pid} if pid == "p1" else None
    )
    monkeypatch.setattr(
        main.store, "read_blocks", lambda pid: BLOCKS if pid == "p1" else None
    )
    monkeypatch.setattr(main.store, "remove", lambda pid: pid == "p1")
    monkeypatch.setattr(main.qa_engine, "needs_external", lambda _q, _s: False)


def _mem(main):
    return memlib.get_store(main.store.root)


def _events(body: str) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for line in body.splitlines()
        if line.startswith("data:")
    ]


def test_ask_persists_turn_and_replays_same_client_id(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)
    backend = MemoryBackend(answer="回答正文")
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            payload = {
                "question": "pairwise structure encoding",
                "web": False,
                "client_id": "c1",
            }
            first = client.post("/api/papers/p1/ask", json=payload)
            assert first.status_code == 200
            body = first.json()
            assert body["answer"] == "回答正文"
            assert body["user_message_id"]
            assert body["answer_message_id"]
            calls_after_first = len(backend.calls)

            chat = client.get("/api/papers/p1/chat")
            assert chat.status_code == 200
            data = chat.json()
            assert data["hasMore"] is False
            assert len(data["turns"]) == 1
            turn = data["turns"][0]
            assert turn["id"] == body["user_message_id"]
            assert turn["question"] == payload["question"]
            assert turn["answer"] == "回答正文"
            assert turn["sources"] == body["sources"]

            again = client.post("/api/papers/p1/ask", json=payload)
            assert again.status_code == 200
            assert again.json() == body
            assert len(backend.calls) == calls_after_first
    finally:
        llm_mod.set_backend(None)

    assert [m["role"] for m in _mem(main).chat("p1")["messages"]] == ["user", "ai"]


def test_ask_stream_event_order_and_persisted_turn(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)
    llm_mod.set_backend(MemoryBackend(pieces=["回答", "正文"]))
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={
                    "question": "pairwise structure encoding",
                    "web": False,
                    "client_id": "c2",
                },
            )
            assert res.status_code == 200
            events = _events(res.text)
            types = [e["type"] for e in events]
            assert types[0] == "status"
            assert events[0]["stage"] == "retrieving"
            assert types[1] == "sources"
            assert types[-1] == "done"
            assert types[2:-1] == ["delta", "delta"]
            assert (
                "".join(e["text"] for e in events if e["type"] == "delta")
                == "回答正文"
            )

            data = client.get("/api/papers/p1/chat").json()
            assert len(data["turns"]) == 1
            assert data["turns"][0]["answer"] == "回答正文"
    finally:
        llm_mod.set_backend(None)

    assert [m["role"] for m in _mem(main).chat("p1")["messages"]] == ["user", "ai"]


def test_ask_stream_includes_recent_turns(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)
    backend = MemoryBackend(pieces=["回答", "正文"])
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            first = client.post(
                "/api/papers/p1/ask",
                json={"question": "first question", "web": False, "client_id": "s1"},
            )
            assert first.status_code == 200

            second = client.post(
                "/api/papers/p1/ask/stream",
                json={"question": "第二个问题", "web": False, "client_id": "s2"},
            )
            assert second.status_code == 200
            events = _events(second.text)
            assert [e["type"] for e in events][-1] == "done"
    finally:
        llm_mod.set_backend(None)

    stream_call = backend.calls[-1]
    user = stream_call[1]["content"]
    assert "【最近对话】" in user
    assert "first question" in user
    assert "回答正文" in user


def test_follow_up_rewrites_with_history_and_injects_memory(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)
    backend = MemoryBackend()
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            first = client.post(
                "/api/papers/p1/ask",
                json={"question": "first question", "web": False, "client_id": "c4"},
            )
            assert first.status_code == 200
            _mem(main).apply_patch(
                "p1",
                {
                    "summary": (
                        "用户已经理解了成对结构编码的基本直觉，正在追问它与普通注意力机制"
                        "在计算成对关系时的差别，尚未确认复杂度是否一致。"
                    )
                },
            )

            second = client.post(
                "/api/papers/p1/ask",
                json={
                    "question": "那它和注意力有什么区别",
                    "web": False,
                    "client_id": "c5",
                },
            )
            assert second.status_code == 200
    finally:
        llm_mod.set_backend(None)

    rewrite_call = backend.calls[2]
    assert "最近对话" in rewrite_call[1]["content"]
    assert "first question" in rewrite_call[1]["content"]

    answer_call = backend.calls[3]
    assert "只用于调整讲法" in answer_call[0]["content"]
    answer_user = answer_call[1]["content"]
    assert answer_user.startswith("【背景记忆】")
    assert "【最近对话】" in answer_user
    assert "first question" in answer_user
    assert "回答正文" in answer_user
    assert (
        answer_user.index("【背景记忆】")
        < answer_user.index("【最近对话】")
        < answer_user.index("【材料】")
    )


def test_delete_paper_removes_memory_file(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)
    llm_mod.set_backend(MemoryBackend(answer="回答正文"))
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask",
                json={
                    "question": "pairwise structure encoding",
                    "web": False,
                    "client_id": "c3",
                },
            )
            assert res.status_code == 200
            path = _mem(main).paper_path("p1")
            assert path.exists()

            deleted = client.delete("/api/papers/p1")
            assert deleted.status_code == 200
            assert not path.exists()
    finally:
        llm_mod.set_backend(None)
