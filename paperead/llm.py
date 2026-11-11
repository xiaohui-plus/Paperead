"""LLM 接入层。

默认走 OpenAI 兼容接口（DeepSeek / SiliconFlow / vLLM / Ollama / OpenAI 均可），
配置全部来自环境变量；宿主应用也可以注入自己的实现：

    from paperead import set_backend

    class MyBackend:
        async def chat(self, messages, *, model=None, temperature=0.25, max_tokens=None) -> str:
            ...

    set_backend(MyBackend())

这样本项目不绑定任何一方 SDK 的调用方式，也方便测试时塞假实现。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, Sequence, runtime_checkable

from .env import env

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
# DeepSeek V4.1 Flash（2026-09），原生多模态；旧名 deepseek-chat 仍可用但不推荐
DEFAULT_MODEL = "deepseek-flash"


def _fixed_temperature_model(model: str | None) -> bool:
    """有些新推理模型把 temperature 固定为 1.0，显式传值会报错。

    Kimi K3.1 Pro 的文档明确写了 temperature=1.0 是固定值、建议不要显式传入；
    这里对这些模型不发送 temperature，交给服务端默认值。
    """
    name = (model or "").lower()
    return name.startswith("kimi-k3") or name.startswith("kimi-k2.5")


def _sanitize_proxy_env() -> None:
    """清洗 NO_PROXY 中 httpx 无法解析的条目。

    Windows 上常见 `NO_PROXY=...,::1,[::1]`，httpx 构建 client 时会把 "[::1]"
    当成 host:port 解析，抛 `InvalidURL: Invalid port: ':1]'`，
    导致所有 LLM 调用在建连接阶段就失败，且报错信息完全联想不到代理配置。
    """
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        kept = [
            p.strip() for p in raw.split(",")
            if p.strip() and ":" not in p and "[" not in p
        ]
        os.environ[key] = ",".join(kept)


_sanitize_proxy_env()


@dataclass
class LLMConfig:
    """LLM 连接配置（默认从环境变量读取）。"""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = 300.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            api_key=(
                env("PAPEREAD_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            ),
            base_url=(
                env("PAPEREAD_BASE_URL")
                or os.getenv("OPENAI_BASE_URL")
                or DEFAULT_BASE_URL
            ),
            model=(
                env("PAPEREAD_MODEL")
                or os.getenv("OPENAI_MODEL")
                or DEFAULT_MODEL
            ),
            timeout=float(env("PAPEREAD_TIMEOUT", "300")),
        )


@runtime_checkable
class LLMBackend(Protocol):
    """后端只需实现这一个方法。"""

    async def chat(
        self,
        messages: Sequence[dict],
        *,
        model: str | None = None,
        temperature: float = 0.25,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> str:
        ...


@dataclass
class ToolCall:
    """模型要求执行的一次工具调用。"""

    id: str
    name: str
    arguments: str = ""


@dataclass
class ToolReply:
    """一轮带 tools 的模型回复：要么是正文，要么是工具调用（也可能两者都有）。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""


class OpenAICompatBackend:
    """OpenAI 兼容接口实现。"""

    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig.from_env()
        if not self.config.api_key:
            raise RuntimeError(
                "未配置 LLM API Key：请设置环境变量 PAPEREAD_API_KEY（或 OPENAI_API_KEY），"
                "或调用 paperead.set_backend() 注入自定义实现。"
            )
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout,
        )

    async def chat(
        self,
        messages: Sequence[dict],
        *,
        model: str | None = None,
        temperature: float = 0.25,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> str:
        actual_model = model or self.config.model
        kwargs: dict[str, object] = {
            "model": actual_model,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        if not _fixed_temperature_model(actual_model):
            kwargs["temperature"] = temperature
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = await self._client.chat.completions.create(**kwargs)
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            # 推理模型的 reasoning token 也计入 max_tokens，给小了会只思考不输出
            raise RuntimeError("模型返回空内容（可能 max_tokens 被 reasoning 耗尽）")
        return content

    async def chat_stream(
        self,
        messages: Sequence[dict],
        *,
        model: str | None = None,
        temperature: float = 0.25,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> AsyncIterator[str]:
        """流式版本：按 delta 产出文本，调用方负责拼接。"""
        actual_model = model or self.config.model
        kwargs: dict[str, object] = {
            "model": actual_model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if not _fixed_temperature_model(actual_model):
            kwargs["temperature"] = temperature
        if extra_body:
            kwargs["extra_body"] = extra_body
        stream = await self._client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                # 部分兼容实现会在末尾补一个只带 usage 的 chunk
                if not getattr(chunk, "choices", None):
                    continue
                text = getattr(chunk.choices[0].delta, "content", None)
                if text:
                    yield text
        finally:
            await stream.close()

    async def chat_tools(
        self,
        messages: Sequence[dict],
        *,
        tools: Sequence[dict],
        model: str | None = None,
        temperature: float = 0.25,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> ToolReply:
        """带 tools 的一轮调用（非流式），供 agent 循环做工具决策。"""
        actual_model = model or self.config.model
        kwargs: dict[str, object] = {
            "model": actual_model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "tools": list(tools),
            "tool_choice": "auto",
        }
        if not _fixed_temperature_model(actual_model):
            kwargs["temperature"] = temperature
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for i, call in enumerate(getattr(msg, "tool_calls", None) or []):
            fn = getattr(call, "function", None)
            calls.append(
                ToolCall(
                    id=getattr(call, "id", "") or f"call_{i}",
                    name=getattr(fn, "name", "") or "",
                    arguments=getattr(fn, "arguments", "") or "",
                )
            )
        return ToolReply(
            content=(msg.content or "").strip(),
            tool_calls=[c for c in calls if c.name],
            finish_reason=choice.finish_reason or "",
        )

    async def chat_stream_tools(
        self,
        messages: Sequence[dict],
        *,
        tools: Sequence[dict],
        model: str | None = None,
        temperature: float = 0.25,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> AsyncIterator[dict]:
        """流式工具调用：边产出正文 delta，最后产出聚合后的 tool_calls。

        不同 OpenAI 兼容网关对 `delta.tool_calls` 的分片方式不一致（有的只发
        一次完整调用，有的按 index 增量拼接），这里统一按 index 累加。
        """
        actual_model = model or self.config.model
        kwargs: dict[str, object] = {
            "model": actual_model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "stream": True,
            "tools": list(tools),
            "tool_choice": "auto",
        }
        if not _fixed_temperature_model(actual_model):
            kwargs["temperature"] = temperature
        if extra_body:
            kwargs["extra_body"] = extra_body

        stream = await self._client.chat.completions.create(**kwargs)
        order: list[int] = []
        acc: dict[int, ToolCall] = {}
        content: list[str] = []
        finish = ""
        try:
            async for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                finish = choice.finish_reason or finish
                delta = choice.delta
                text = getattr(delta, "content", None)
                if text:
                    content.append(text)
                    yield {"type": "text", "text": text}
                for call in getattr(delta, "tool_calls", None) or []:
                    idx = getattr(call, "index", None)
                    if idx is None:
                        idx = 0
                    current = acc.get(idx)
                    if current is None:
                        current = ToolCall(id="", name="", arguments="")
                        acc[idx] = current
                        order.append(idx)
                    cid = getattr(call, "id", None)
                    if cid:
                        current.id = cid
                    fn = getattr(call, "function", None)
                    if fn is not None:
                        name = getattr(fn, "name", None)
                        if name:
                            current.name += name
                        args = getattr(fn, "arguments", None)
                        if args:
                            current.arguments += args
        finally:
            await stream.close()

        calls: list[ToolCall] = []
        for i, idx in enumerate(order):
            call = acc[idx]
            if not call.name:
                continue
            if not call.id:
                call.id = f"call_{i}"
            calls.append(call)
        yield {
            "type": "reply",
            "content": "".join(content),
            "tool_calls": calls,
            "finish_reason": finish or ("tool_calls" if calls else "stop"),
        }


_backend: LLMBackend | None = None


def set_backend(backend: LLMBackend | None) -> None:
    """注入/重置全局后端（传 None 表示恢复默认的 OpenAI 兼容实现）。"""
    global _backend
    _backend = backend


def get_backend() -> LLMBackend:
    global _backend
    if _backend is None:
        _backend = OpenAICompatBackend()
    return _backend


def active_model() -> str | None:
    """当前全局后端实际使用的模型名（翻译等任务据此决定是否关思考）。"""
    return getattr(getattr(get_backend(), "config", None), "model", None)


async def chat(
    messages: Sequence[dict],
    *,
    model: str | None = None,
    temperature: float = 0.25,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
) -> str:
    """调用当前后端，返回纯文本内容。"""
    kwargs: dict = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    return await get_backend().chat(messages, **kwargs)


async def chat_stream(
    messages: Sequence[dict],
    *,
    model: str | None = None,
    temperature: float = 0.25,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
) -> AsyncIterator[str]:
    """流式调用当前后端，逐段产出文本。

    ``LLMBackend`` 只要求实现 ``chat``；自定义后端如果没有 ``chat_stream``，
    这里退化成一次性返回整段文本，调用方逻辑不需要区分。
    """
    backend = get_backend()
    kwargs: dict = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    streamer = getattr(backend, "chat_stream", None)
    if streamer is not None:
        async for piece in streamer(messages, **kwargs):
            if piece:
                yield piece
        return

    text = await backend.chat(messages, **kwargs)
    if text:
        yield text

def supports_tools() -> bool:
    """当前后端是否支持工具调用；不支持时调用方应回退到固定检索管道。"""
    return hasattr(get_backend(), "chat_tools")


async def chat_tools(
    messages: Sequence[dict],
    *,
    tools: Sequence[dict],
    model: str | None = None,
    temperature: float = 0.25,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
) -> ToolReply:
    backend = get_backend()
    fn = getattr(backend, "chat_tools", None)
    if fn is None:
        raise NotImplementedError("当前模型后端不支持 tools")
    kwargs: dict = {
        "tools": tools,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    return await fn(messages, **kwargs)


async def chat_stream_tools(
    messages: Sequence[dict],
    *,
    tools: Sequence[dict],
    model: str | None = None,
    temperature: float = 0.25,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
) -> AsyncIterator[dict]:
    """流式工具调用事件流：`{"type":"text"}` 与 `{"type":"reply"}`。

    后端没有流式工具能力时退回一次性 `chat_tools`，调用方逻辑不变。
    """
    backend = get_backend()
    fn = getattr(backend, "chat_stream_tools", None)
    kwargs: dict = {
        "tools": tools,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body is not None:
        kwargs["extra_body"] = extra_body

    if fn is not None:
        async for event in fn(messages, **kwargs):
            yield event
        return

    reply = await chat_tools(messages, **kwargs)
    if reply.content:
        yield {"type": "text", "text": reply.content}
    yield {
        "type": "reply",
        "content": reply.content,
        "tool_calls": reply.tool_calls,
        "finish_reason": reply.finish_reason or ("tool_calls" if reply.tool_calls else "stop"),
    }
