"""流式问答 SSE 的端到端测试（假后端，不依赖真实模型）。"""

import importlib
import json

from fastapi.testclient import TestClient

from paper_reflow import llm as llm_mod


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


class StreamBackend:
    """带流式能力的假后端。"""

    def __init__(self, pieces, fail_after=None):
        self.pieces = pieces
        self.fail_after = fail_after

    async def chat(self, messages, **kwargs):
        return "".join(self.pieces)

    async def chat_stream(self, messages, **kwargs):
        for i, piece in enumerate(self.pieces):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("模型连接中断")
            yield piece


class PlainBackend:
    """只实现 chat 的自定义后端：应退化成一次性输出。"""

    async def chat(self, messages, **kwargs):
        return "一次性回答"


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_REFLOW_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPER_REFLOW_MOCK", "1")
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

    async def no_expand(_question):
        return []

    monkeypatch.setattr(main.qa_engine, "expand_query", no_expand)
    monkeypatch.setattr(main.qa_engine, "needs_external", lambda _q, _s: False)


def _events(body: str) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for line in body.splitlines()
        if line.startswith("data:")
    ]


def _post_stream(main, question="pairwise structure encoding"):
    with TestClient(main.app) as client:
        return client.post(
            "/api/papers/p1/ask/stream",
            json={"question": question, "web": False},
        )


def test_ask_stream_event_order_and_text(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    pieces = ["成对结构编码", "捕获两个目标节点", "之间的结构特征。"]
    llm_mod.set_backend(StreamBackend(pieces))
    try:
        res = _post_stream(main)
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _events(res.text)
    types = [e["type"] for e in events]
    assert types[0] == "status"
    assert events[0]["stage"] == "retrieving"
    assert types[1] == "sources"
    assert types[-1] == "done"
    assert events[-1]["latency"] >= 0

    assert "".join(e["text"] for e in events if e["type"] == "delta") == "".join(pieces)
    assert events[1]["sources"][0]["label"] == "P1"
    assert events[1]["usedWeb"] is False


def test_ask_stream_falls_back_to_plain_chat(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    llm_mod.set_backend(PlainBackend())
    try:
        res = _post_stream(main)
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    events = _events(res.text)
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["一次性回答"]
    assert [e["type"] for e in events][-1] == "done"


def test_ask_stream_emits_error_but_keeps_partial_text(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    llm_mod.set_backend(StreamBackend(["前半段", "后半段"], fail_after=1))
    try:
        res = _post_stream(main)
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    events = _events(res.text)
    types = [e["type"] for e in events]
    assert "done" not in types
    assert types[-1] == "error"
    assert "模型连接中断" in events[-1]["message"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "前半段"


def test_ask_stream_requires_model_config(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)
    monkeypatch.delenv("PAPER_REFLOW_API_KEY", raising=False)

    res = _post_stream(main)
    assert res.status_code == 400
    assert "未配置模型" in res.json()["detail"]
