"""阅读页问答的 agent 循环：让模型自己决定检索什么。

参考 DSH 的两段式网页能力：
- `web_search` 只返回 URL 与摘要，模型判断需要正文时再调 `web_fetch`；
- 工具调用轮数有硬上限，避免模型反复搜索把一次问答拖成十几轮；
- 事件流与研究页同构（tool_start / tool_end / text），前端直接渲染成工具卡片。

这里只实现循环与协议，具体的论文检索、联网、抓正文由调用方注入 executor，
这样 `paperead` 不依赖 server 的存储与路由。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from ..llm import chat_stream_tools

log = logging.getLogger(__name__)

MAX_STEPS = 6


@dataclass
class AgentTool:
    """一个暴露给模型的工具。"""

    name: str
    description: str
    parameters: dict

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolOutcome:
    """工具执行结果：`content` 给模型，`items` 给前端的工具卡片。"""

    content: str
    summary: str = ""
    items: list[dict] = field(default_factory=list)
    status: str = "done"  # done | error
    error: str = ""
    meta: dict = field(default_factory=dict)


ToolExecutor = Callable[[str, dict], Awaitable[ToolOutcome]]


def _parse_args(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:  # noqa: BLE001
        # 少数模型会把参数包在 ```json 里
        import re

        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
        except Exception:  # noqa: BLE001
            return {}
    return value if isinstance(value, dict) else {}


def _assistant_tool_message(content: str, calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": calls,
    }


async def run_agent(
    *,
    system: str,
    question: str,
    tools: Sequence[AgentTool],
    executor: ToolExecutor,
    history: list[dict] | None = None,
    max_steps: int = MAX_STEPS,
    max_tokens: int = 4000,
    temperature: float = 0.2,
) -> AsyncIterator[tuple[str, object]]:
    """产出 `("tool_start"|"tool_end"|"text"|"final", payload)`。

    - 工具轮如果模型同时写了正文，正文会照常向上游转发（与研究页一致，
      模型一般会在 tool 轮只输出调用，不写正文）；
    - 达到轮数上限后用一次不带 tools 的流式调用收口，保证一定有答案。
    """
    tool_defs = [tool.to_openai() for tool in tools]
    messages: list[dict] = [{"role": "system", "content": system}]
    for item in history or []:
        role = item.get("role")
        text = item.get("text") or item.get("content") or ""
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": question})

    async def stream_round(with_tools: bool) -> AsyncIterator[tuple[str, object]]:
        text_parts: list[str] = []
        reply: dict | None = None
        async for event in chat_stream_tools(
            messages,
            tools=tool_defs if with_tools else [],
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            if event.get("type") == "text":
                piece = event.get("text") or ""
                if piece:
                    text_parts.append(piece)
                    yield ("text", piece)
            elif event.get("type") == "reply":
                reply = event
        if reply is None:
            raise RuntimeError("模型没有返回内容")
        yield ("reply", {"reply": reply, "text": "".join(text_parts)})

    for step in range(max_steps):
        reply: dict | None = None
        text = ""
        async for kind, payload in stream_round(True):
            if kind == "reply":
                reply = payload["reply"]  # type: ignore[index]
                text = payload["text"]  # type: ignore[index]
            else:
                yield (kind, payload)
        if reply is None:
            raise RuntimeError("模型没有返回内容")

        calls = [c for c in (reply.get("tool_calls") or []) if getattr(c, "name", "")]
        if not calls:
            yield ("final", text or reply.get("content") or "")
            return

        tool_calls_payload = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments or "{}"},
            }
            for call in calls
        ]
        messages.append(_assistant_tool_message(text, tool_calls_payload))

        for call in calls:
            args = _parse_args(call.arguments)
            yield ("tool_start", {"callId": call.id, "name": call.name, "input": args})
            try:
                outcome = await executor(call.name, args)
            except Exception as e:  # noqa: BLE001
                log.exception("工具执行失败 %s", call.name)
                outcome = ToolOutcome(
                    content=f"工具执行失败：{e}",
                    status="error",
                    error=str(e)[:300],
                    summary="执行失败",
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": outcome.content or "（没有内容）",
                }
            )
            yield (
                "tool_end",
                {
                    "callId": call.id,
                    "name": call.name,
                    "status": outcome.status,
                    "summary": outcome.summary,
                    "items": outcome.items,
                    "error": outcome.error,
                    "meta": outcome.meta,
                },
            )

    # 轮数用尽：让模型基于已有材料直接回答，不再给工具
    messages.append(
        {
            "role": "user",
            "content": "工具调用次数已到上限。请只根据上面已经拿到的材料回答，不要再请求工具；材料不足就说明缺什么。",
        }
    )
    text_parts: list[str] = []
    async for event in chat_stream_tools(
        messages, tools=[], temperature=temperature, max_tokens=max_tokens
    ):
        if event.get("type") == "text":
            piece = event.get("text") or ""
            if piece:
                text_parts.append(piece)
                yield ("text", piece)
    yield ("final", "".join(text_parts))