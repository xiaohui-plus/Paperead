"""阅读问答 agent 模式的 SSE 端到端测试：工具事件、来源汇总与历史落盘。"""

from __future__ import annotations

import importlib
import json

from fastapi.testclient import TestClient

from paperead import llm as llm_mod
from paperead.llm import ToolCall

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
            "paths as heuristic features, then encodes them with attention before pooling."
        ),
        "bbox": [0, 30, 100, 60],
        "translated": (
            "成对结构编码捕获两个目标节点之间的结构特征，使用最短路径距离和最短路数量"
            "作为启发式特征，再由注意力编码后池化。"
        ),
    },
]


class AgentBackend:
    """脚本化 agent：paper_search → web_search → web_fetch → 回答。"""

    def __init__(self) -> None:
        self.round = 0
        self.seen: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        return "一次性回答"

    async def chat_stream(self, messages, **kwargs):
        yield "一次性回答"

    async def chat_tools(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("agent 应优先走流式工具调用")

    async def chat_stream_tools(self, messages, *, tools=(), **kwargs):
        self.seen.append([dict(m) for m in messages])
        self.tools_seen.append(list(tools))
        self.round += 1
        if self.round == 1:
            calls = [
                ToolCall(
                    id="c1",
                    name="paper_search",
                    arguments='{"query":"pairwise structure encoding"}',
                )
            ]
            yield {"type": "reply", "content": "", "tool_calls": calls, "finish_reason": "tool_calls"}
            return
        if self.round == 2:
            calls = [
                ToolCall(id="c2", name="web_search", arguments='{"query":"pairwise structure"}'),
                ToolCall(id="c3", name="web_fetch", arguments='{"url":"https://e.com/a"}'),
            ]
            yield {"type": "reply", "content": "", "tool_calls": calls, "finish_reason": "tool_calls"}
            return
        text = "成对结构编码用最短路径距离刻画节点关系。[P1]\n\n公开资料补充了注意力池化。[W1]"
        for ch in text:
            yield {"type": "text", "text": ch}
        yield {"type": "reply", "content": text, "tool_calls": [], "finish_reason": "stop"}


class PlainBackend:
    async def chat(self, messages, **kwargs):
        return "固定管道回答"

    async def chat_stream(self, messages, **kwargs):
        yield "固定管道回答"


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.setenv("PAPEREAD_API_KEY", "test-key")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)
    monkeypatch.delenv("PAPEREAD_TUTOR_AGENT", raising=False)

    import server.main as main

    importlib.reload(main)
    return main


def _use_fake_paper(main, monkeypatch):
    monkeypatch.setattr(
        main.store, "get", lambda pid: {"id": pid, "title": "图结构编码", "pages": 8}
    )
    monkeypatch.setattr(main.store, "read_blocks", lambda pid: BLOCKS)

    async def fake_search_many(*args, **kwargs):
        return [
            {
                "title": "Pairwise structure encoding 详解",
                "url": "https://e.com/a",
                "content": "用最短路径距离与路径数量刻画节点间结构关系。",
                "engine": "so360",
            }
        ]

    async def fake_academic(query, limit=3):
        return []

    async def fake_fetch(url, max_chars=2400):
        return {
            "url": url,
            "statusCode": 200,
            "content": "成对结构编码把两个节点的最短路径作为特征，再用注意力池化。",
            "truncated": False,
            "title": "结构编码入门",
            "error": "",
        }

    monkeypatch.setattr(main.web_search, "search_many", fake_search_many)
    monkeypatch.setattr(main.web_search, "search_academic", fake_academic)
    monkeypatch.setattr(main.web_search, "fetch", fake_fetch)


def _events(body: str) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for line in body.splitlines()
        if line.startswith("data:")
    ]


def test_agent_stream_emits_three_tools_and_persists(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)
    backend = AgentBackend()
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={"question": "成对结构编码怎么理解", "web": True, "client_id": "a1"},
            )
            history = client.get("/api/papers/p1/chat").json()
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    events = _events(res.text)
    types = [e["type"] for e in events]
    assert types[0] == "status"
    assert types[-1] == "done"

    tools = [e for e in events if e["type"] == "tool"]
    names = [e["name"] for e in tools if e.get("phase") == "end"]
    assert names == ["paper_search", "web_search", "web_fetch"]
    fetch_end = next(e for e in tools if e.get("name") == "web_fetch" and e.get("phase") == "end")
    assert fetch_end["items"][0]["url"] == "https://e.com/a"
    assert fetch_end["status"] == "done"

    sources_ev = next(e for e in events if e["type"] == "sources")
    assert [t["name"] for t in sources_ev["tools"]] == names
    assert sources_ev["webStatus"]["count"] == 1

    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "成对结构编码" in deltas

    assert len(history["turns"]) == 1
    assert [t["name"] for t in history["turns"][0]["tools"]] == names

    # 工具结果必须回填到模型消息里
    last_round = backend.seen[-1]
    role_seq = [m.get("role") for m in last_round]
    assert "tool" in role_seq
    assert any("【网页】结构编码入门" in (m.get("content") or "") for m in last_round)


def test_agent_disabled_falls_back_to_fixed_pipeline(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)
    monkeypatch.setenv("PAPEREAD_TUTOR_AGENT", "0")

    backend = PlainBackend()
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={"question": "成对结构编码是什么", "web": False},
            )
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    events = _events(res.text)
    names = [e["name"] for e in events if e["type"] == "tool" and e.get("phase") == "end"]
    assert names == ["paper_search"]


def test_agent_failure_before_output_falls_back(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    class BrokenAgent:
        async def chat(self, messages, **kwargs):
            return "固定管道回答"

        async def chat_stream(self, messages, **kwargs):
            yield "固定管道回答"

        async def chat_tools(self, messages, **kwargs):
            raise RuntimeError("tools unsupported")

        async def chat_stream_tools(self, messages, *, tools=(), **kwargs):
            raise RuntimeError("HTTP 400: tools is not supported")
            yield  # pragma: no cover

    llm_mod.set_backend(BrokenAgent())
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={"question": "成对结构编码是什么", "web": False},
            )
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    events = _events(res.text)
    assert [e["type"] for e in events][-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "固定管道回答"
    # 回退后仍应有固定管道的工具卡片
    assert any(e["type"] == "tool" for e in events)