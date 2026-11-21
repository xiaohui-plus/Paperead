"""流式问答 SSE 的端到端测试（假后端，不依赖真实模型）。"""

import importlib
import json

from fastapi.testclient import TestClient

from paperead import llm as llm_mod


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
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "lib"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    monkeypatch.setenv("PAPEREAD_API_KEY", "test-key")
    monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)

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

    async def no_fetch(url, max_chars=2400):
        return {
            "url": url,
            "statusCode": 0,
            "content": "",
            "truncated": False,
            "title": "",
            "error": "测试默认不抓正文",
        }

    # 深读只在论文材料不足时触发；默认关掉真实抓取，避免测试打外网
    monkeypatch.setattr(main.web_search, "fetch", no_fetch)

    async def no_many(*args, **kwargs):
        return []

    # 材料不足时会主动扩检；测试里默认不追加，需要的用例自己覆盖
    monkeypatch.setattr(main.web_search, "search_many", no_many)


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
    # 检索步骤先以工具卡片事件到达，再给出出处汇总
    assert "tool" in types
    sources_index = types.index("sources")
    assert "tool" in types[:sources_index]
    assert types[-1] == "done"
    assert events[-1]["latency"] >= 0

    assert "".join(e["text"] for e in events if e["type"] == "delta") == "".join(pieces)
    sources_ev = events[sources_index]
    assert sources_ev["sources"][0]["label"] == "P1"
    assert sources_ev["usedWeb"] is False
    assert sources_ev["tools"][0]["name"] == "paper_context"
    assert sources_ev["tools"][0]["status"] == "done"

    paper_end = next(
        e for e in events if e["type"] == "tool" and e.get("phase") == "end"
    )
    assert paper_end["name"] == "paper_context"
    assert paper_end["meta"]["chunks"] >= 1
    assert paper_end["items"][0]["label"] == "P1"


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
    monkeypatch.delenv("PAPEREAD_API_KEY", raising=False)

    res = _post_stream(main)
    assert res.status_code == 400
    assert "未配置模型" in res.json()["detail"]

class CapturingBackend:
    def __init__(self, pieces):
        self.pieces = pieces
        self.seen: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        return "".join(self.pieces)

    async def chat_stream(self, messages, **kwargs):
        self.seen.append(list(messages))
        for piece in self.pieces:
            yield piece


def test_ask_stream_includes_web_sources_and_allows_common_sense(monkeypatch, tmp_path):
    """联网结果必须进入出处与提示词，且论文没写时允许用通识回答。"""
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    async def fake_search(query, limit=4):
        return [
            {
                "title": "自注意力机制详解",
                "url": "https://e.com/a",
                "content": "自注意力让模型关注序列中的所有词",
                "engine": "so360",
            },
            {
                "title": "对比学习入门",
                "url": "https://e.com/b",
                "content": "对比学习是一种自监督方法",
                "engine": "so360",
            },
        ]

    monkeypatch.setattr(main.web_search, "search", fake_search)

    backend = CapturingBackend(["自注意力机制", "会同时关注所有词。"])
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={"question": "自注意力机制是什么", "web": True},
            )
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    events = _events(res.text)
    sources_ev = next(e for e in events if e["type"] == "sources")
    assert sources_ev["usedWeb"] is True
    assert sources_ev["webStatus"]["attempted"] is True
    assert sources_ev["webStatus"]["count"] == 2
    assert sources_ev["webStatus"]["engines"] == ["so360"]
    assert [t["name"] for t in sources_ev["tools"]] == [
        "paper_context",
        "web_search",
        "web_fetch",
    ]
    assert sources_ev["tools"][1]["items"][0]["url"] == "https://e.com/a"
    web_sources = [s for s in sources_ev["sources"] if s["type"] == "web"]
    assert [s["url"] for s in web_sources] == ["https://e.com/a", "https://e.com/b"]
    assert web_sources[0]["engine"] == "so360"

    # 检索式来自「缺口规划」，不能把用户原句直接当关键词
    web_start = next(
        e
        for e in events
        if e["type"] == "tool"
        and e.get("name") == "web_search"
        and e.get("phase") == "start"
    )
    assert web_start["input"]["query"]
    assert web_start["input"]["query"] != "自注意力机制是什么"
    assert "是什么" not in web_start["input"]["query"]
    assert web_start["input"]["goal"]

    user_prompt = backend.seen[0][-1]["content"]
    assert "[W1] 自注意力机制详解" in user_prompt
    assert "https://e.com/a" in user_prompt


def test_ask_stream_includes_user_quote(monkeypatch, tmp_path):
    """阅读页划词「提问」的引用片段必须进入提示词材料。"""
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    backend = CapturingBackend(["这句话讲的是成对结构编码。"])
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={
                    "question": "这句话在说什么",
                    "web": False,
                    "quotes": [
                        {
                            "text": "It uses shortest path distance",
                            "page": 1,
                            "block_id": "b2",
                            "source": "en",
                        }
                    ],
                },
            )
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    user_prompt = backend.seen[0][-1]["content"]
    assert "用户选中的原文片段" in user_prompt
    assert "It uses shortest path distance" in user_prompt
    assert "第 1 页" in user_prompt


def test_ask_stream_reports_web_attempt_without_results(monkeypatch, tmp_path):
    """联网开关打开但没搜到结果时，也要把 attempted/count 透传给前端。"""
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    async def empty_search(query, limit=4):
        return []

    async def empty_many(*args, **kwargs):
        return []

    monkeypatch.setattr(main.web_search, "search", empty_search)
    monkeypatch.setattr(main.web_search, "search_many", empty_many)

    backend = CapturingBackend(["论文没有直接写，", "这里用通识解释。"])
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={"question": "自注意力机制是什么", "web": True},
            )
    finally:
        llm_mod.set_backend(None)

    events = _events(res.text)
    sources_ev = next(e for e in events if e["type"] == "sources")
    assert sources_ev["usedWeb"] is False
    status = sources_ev["webStatus"]
    assert status["attempted"] is True
    assert status["count"] == 0
    assert status["engines"] == []
    assert status["engine"] == ""
    assert status["goal"]
    user_prompt = backend.seen[0][-1]["content"]
    assert "【联网状态】" in user_prompt
    assert "（通识）" in user_prompt

def test_ask_stream_deep_reads_when_paper_lacks_answer(monkeypatch, tmp_path):
    """论文材料不足且联网可用时，自动抓网页正文补充，而不是只靠摘要或拒答。"""
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    async def fake_search(query, limit=4):
        return [
            {
                "title": "幻觉缓解综述",
                "url": "https://e.com/a",
                "content": "论文外的摘要 A",
                "engine": "so360",
            },
            {
                "title": "RAG 实践",
                "url": "https://e.com/b",
                "content": "论文外的摘要 B",
                "engine": "so360",
            },
        ]

    async def empty_many(*args, **kwargs):
        return []

    async def fake_fetch(url, max_chars=2400):
        if url.endswith("/b"):
            return {
                "url": url,
                "statusCode": 403,
                "content": "",
                "truncated": False,
                "title": "",
                "error": "403",
            }
        return {
            "url": url,
            "statusCode": 200,
            "content": "缓解幻觉的常用做法包括检索增强与事实校验。",
            "truncated": False,
            "title": "幻觉缓解综述",
            "error": "",
        }

    monkeypatch.setattr(main.web_search, "search", fake_search)
    monkeypatch.setattr(main.web_search, "search_many", empty_many)
    monkeypatch.setattr(main.web_search, "fetch", fake_fetch)

    backend = CapturingBackend(["论文材料不足，", "这里用联网正文补充。"])
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={
                    "question": "多模态大模型的幻觉问题怎么缓解",
                    "web": True,
                },
            )
    finally:
        llm_mod.set_backend(None)

    assert res.status_code == 200
    events = _events(res.text)
    sources_ev = next(e for e in events if e["type"] == "sources")
    assert sources_ev["usedWeb"] is True
    assert sources_ev["webStatus"]["depth"] == "deep"
    assert sources_ev["webStatus"]["fetched"] == 1

    names = [t["name"] for t in sources_ev["tools"]]
    assert names == ["paper_context", "web_search", "web_fetch"]
    fetch_tool = next(t for t in sources_ev["tools"] if t["name"] == "web_fetch")
    assert fetch_tool["status"] == "done"
    assert "深读 1 篇" in fetch_tool["summary"]
    assert fetch_tool["items"][0]["url"] == "https://e.com/a"

    user_prompt = backend.seen[0][-1]["content"]
    assert "[W1 正文]" in user_prompt
    assert "检索增强与事实校验" in user_prompt
    assert "RAG 实践" in user_prompt
    assert "论文外的摘要 B" in user_prompt

def test_ask_stream_skips_web_when_paper_already_covers_question(monkeypatch, tmp_path):
    """论文档案能回答的问题不得联网：没有联网工具卡片，也不合成检索式。"""
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    backend = CapturingBackend(["成对结构编码使用最短路径距离与最短路数量。"])
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={
                    "question": "What does pairwise structure encoding use?",
                    "web": True,
                },
            )
    finally:
        llm_mod.set_backend(None)

    events = _events(res.text)
    sources_ev = next(e for e in events if e["type"] == "sources")
    assert sources_ev["usedWeb"] is False
    assert sources_ev["webStatus"]["attempted"] is False
    assert sources_ev["webStatus"]["skipped"] is True
    assert [t["name"] for t in sources_ev["tools"]] == ["paper_context"]
    # 主动不联网时不能给模型「联网失败、只能用通识」的误导提示
    user_prompt = backend.seen[0][-1]["content"]
    assert "【联网状态】" not in user_prompt


def test_ask_stream_respects_planner_when_it_says_paper_is_enough(monkeypatch, tmp_path):
    """规划器判定论文材料足够时，即使问题带「是什么」也不联网。"""
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    async def plan_chat(messages, **kwargs):
        return '{"needed": false, "goal": "论文已经解释了该结构"}'

    monkeypatch.setattr(main.qa_engine, "chat", plan_chat)

    backend = CapturingBackend(["论文里已解释。"])
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={"question": "自注意力机制是什么", "web": True},
            )
    finally:
        llm_mod.set_backend(None)

    events = _events(res.text)
    sources_ev = next(e for e in events if e["type"] == "sources")
    assert sources_ev["usedWeb"] is False
    assert sources_ev["webStatus"]["attempted"] is False
    assert sources_ev["webStatus"]["reason"] == "论文已经解释了该结构"


def test_ask_stream_searches_with_planned_query_not_user_question(monkeypatch, tmp_path):
    """需要联网时按规划出的检索式搜索，而不是把用户原句交给搜索引擎。"""
    main = _load_main(monkeypatch, tmp_path)
    _use_fake_paper(main, monkeypatch)

    seen_queries: list[list[str]] = []

    async def plan_chat(messages, **kwargs):
        return (
            '{"needed": true, "goal": "论文未解释幻觉缓解机制",'
            ' "queries": ["多模态大模型 幻觉 缓解 综述"],'
            ' "academic_query": "multimodal hallucination mitigation"}'
        )

    async def fake_many(queries, **kwargs):
        seen_queries.append(list(queries))
        return [
            {
                "title": "幻觉缓解综述",
                "url": "https://e.com/a",
                "content": "检索增强与事实校验可缓解幻觉",
                "engine": "so360",
            }
        ]

    monkeypatch.setattr(main.qa_engine, "chat", plan_chat)
    monkeypatch.setattr(main.web_search, "search_many", fake_many)

    question = "多模态大模型的幻觉问题怎么缓解"
    backend = CapturingBackend(["用检索增强缓解。"])
    llm_mod.set_backend(backend)
    try:
        with TestClient(main.app) as client:
            res = client.post(
                "/api/papers/p1/ask/stream",
                json={"question": question, "web": True},
            )
    finally:
        llm_mod.set_backend(None)

    assert seen_queries == [["多模态大模型 幻觉 缓解 综述"]]
    assert question not in seen_queries[0]

    events = _events(res.text)
    sources_ev = next(e for e in events if e["type"] == "sources")
    assert sources_ev["usedWeb"] is True
    assert sources_ev["webStatus"]["goal"] == "论文未解释幻觉缓解机制"
    start = next(
        e
        for e in events
        if e["type"] == "tool"
        and e.get("name") == "web_search"
        and e.get("phase") == "start"
    )
    assert start["input"]["queries"] == ["多模态大模型 幻觉 缓解 综述"]
