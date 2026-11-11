"""阅读问答 agent 循环测试：多轮工具调用、轮数上限与异常兜底（不联网）。"""

from __future__ import annotations

import asyncio

import pytest

from paperead.llm import ToolCall, set_backend
from paperead.tutor.agent import AgentTool, ToolOutcome, _parse_args, run_agent

TOOLS = [
    AgentTool(
        name="paper_search",
        description="search paper",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
    AgentTool(
        name="web_search",
        description="search web",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
]


class ScriptedBackend:
    """按脚本逐轮返回 tool_calls / 正文。"""

    def __init__(self, rounds: list[dict], final_text: str = ""):
        self.rounds = list(rounds)
        self.final_text = final_text
        self.calls: list[list[dict]] = []
        self.tools_seen: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        return self.final_text

    async def chat_stream_tools(self, messages, *, tools=(), **kwargs):
        self.calls.append([dict(m) for m in messages])
        self.tools_seen.append(list(tools))
        if not tools:
            # 轮数用尽后的收口调用（不再暴露工具）
            for ch in self.final_text:
                yield {"type": "text", "text": ch}
            yield {
                "type": "reply",
                "content": self.final_text,
                "tool_calls": [],
                "finish_reason": "stop",
            }
            return
        if not self.rounds:
            yield {
                "type": "reply",
                "content": self.final_text,
                "tool_calls": [],
                "finish_reason": "stop",
            }
            return
        round_ = self.rounds.pop(0)
        text = round_.get("text", "")
        for ch in text:
            yield {"type": "text", "text": ch}
        yield {
            "type": "reply",
            "content": text,
            "tool_calls": round_.get("tool_calls", []),
            "finish_reason": "tool_calls" if round_.get("tool_calls") else "stop",
        }


@pytest.fixture(autouse=True)
def reset_backend():
    yield
    set_backend(None)


def _collect(gen) -> list[tuple[str, object]]:
    async def go():
        return [item async for item in gen]

    return asyncio.run(go())


def test_run_agent_executes_tools_then_answers():
    backend = ScriptedBackend(
        [
            {
                "tool_calls": [
                    ToolCall(id="c1", name="paper_search", arguments='{"query":"扩散模型"}')
                ]
            },
            {
                "tool_calls": [
                    ToolCall(id="c2", name="web_search", arguments='{"query":"diffusion"}')
                ]
            },
            {"text": "最终回答"},
        ]
    )
    set_backend(backend)

    calls: list[tuple[str, dict]] = []

    async def executor(name: str, args: dict) -> ToolOutcome:
        calls.append((name, args))
        return ToolOutcome(content=f"{name} 结果", summary=f"{name} 完成")

    events = _collect(
        run_agent(
            system="sys",
            question="扩散模型是什么",
            tools=TOOLS,
            executor=executor,
            history=[{"role": "user", "content": "上一轮"}, {"role": "assistant", "content": "上一答"}],
        )
    )
    kinds = [kind for kind, _ in events]
    assert kinds == [
        "tool_start",
        "tool_end",
        "tool_start",
        "tool_end",
        "text",
        "text",
        "text",
        "text",
        "final",
    ]
    assert calls == [("paper_search", {"query": "扩散模型"}), ("web_search", {"query": "diffusion"})]
    assert events[-1] == ("final", "最终回答")

    # 工具结果必须以 tool 消息回填，模型才能看到
    second_round = backend.calls[1]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in second_round)
    assert second_round[0]["role"] == "system"
    assert second_round[1]["content"] == "上一轮"
    assert second_round[2]["content"] == "上一答"


def test_run_agent_stops_at_step_limit_and_finalizes_without_tools():
    # 每一轮都要求调工具，max_steps=2 后必须收口，且收口调用不再暴露 tools
    rounds = [
        {"tool_calls": [ToolCall(id=f"c{i}", name="paper_search", arguments='{"query":"x"}')]}
        for i in range(5)
    ]
    backend = ScriptedBackend(rounds, final_text="收口回答")
    set_backend(backend)

    async def executor(name: str, args: dict) -> ToolOutcome:
        return ToolOutcome(content="ok", summary="ok")

    events = _collect(
        run_agent(
            system="sys",
            question="q",
            tools=TOOLS,
            executor=executor,
            max_steps=2,
        )
    )
    assert events[-1] == ("final", "收口回答")
    assert backend.tools_seen[-1] == []
    assert len([k for k, _ in events if k == "tool_start"]) == 2
    # 收口提示已加入消息
    assert any("工具调用次数已到上限" in (m.get("content") or "") for m in backend.calls[-1])


def test_run_agent_converts_tool_exception_to_error_event():
    backend = ScriptedBackend(
        [
            {"tool_calls": [ToolCall(id="c1", name="paper_search", arguments='{"query":"x"}')]},
            {"text": "已根据现有材料回答"},
        ]
    )
    set_backend(backend)

    async def executor(name: str, args: dict) -> ToolOutcome:
        raise RuntimeError("检索炸了")

    events = _collect(
        run_agent(system="sys", question="q", tools=TOOLS, executor=executor)
    )
    end = next(payload for kind, payload in events if kind == "tool_end")
    assert end["status"] == "error"
    assert "检索炸了" in end["error"]
    assert events[-1][0] == "final"


def test_parse_args_tolerates_fenced_json_and_garbage():
    assert _parse_args('{"query": "x"}') == {"query": "x"}
    assert _parse_args('```json\n{"query": "y"}\n```') == {"query": "y"}
    assert _parse_args("") == {}
    assert _parse_args("not json") == {}
    assert _parse_args("[1,2]") == {}