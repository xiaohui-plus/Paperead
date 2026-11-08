"""LLM 接入层。

默认走 OpenAI 兼容接口（DeepSeek / SiliconFlow / vLLM / Ollama / OpenAI 均可），
配置全部来自环境变量；宿主应用也可以注入自己的实现：

    from paper_reflow import set_backend

    class MyBackend:
        async def chat(self, messages, *, model=None, temperature=0.25, max_tokens=None) -> str:
            ...

    set_backend(MyBackend())

这样本项目不绑定任何一方 SDK 的调用方式，也方便测试时塞假实现。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import AsyncIterator, Protocol, Sequence, runtime_checkable

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
                os.getenv("PAPER_REFLOW_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            ),
            base_url=(
                os.getenv("PAPER_REFLOW_BASE_URL")
                or os.getenv("OPENAI_BASE_URL")
                or DEFAULT_BASE_URL
            ),
            model=(
                os.getenv("PAPER_REFLOW_MODEL")
                or os.getenv("OPENAI_MODEL")
                or DEFAULT_MODEL
            ),
            timeout=float(os.getenv("PAPER_REFLOW_TIMEOUT", "300")),
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
    ) -> str:
        ...


class OpenAICompatBackend:
    """OpenAI 兼容接口实现。"""

    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig.from_env()
        if not self.config.api_key:
            raise RuntimeError(
                "未配置 LLM API Key：请设置环境变量 PAPER_REFLOW_API_KEY（或 OPENAI_API_KEY），"
                "或调用 paper_reflow.set_backend() 注入自定义实现。"
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
    ) -> str:
        actual_model = model or self.config.model
        kwargs: dict[str, object] = {
            "model": actual_model,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        if not _fixed_temperature_model(actual_model):
            kwargs["temperature"] = temperature
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


async def chat(
    messages: Sequence[dict],
    *,
    model: str | None = None,
    temperature: float = 0.25,
    max_tokens: int | None = None,
) -> str:
    """调用当前后端，返回纯文本内容。"""
    return await get_backend().chat(
        messages, model=model, temperature=temperature, max_tokens=max_tokens
    )


async def chat_stream(
    messages: Sequence[dict],
    *,
    model: str | None = None,
    temperature: float = 0.25,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """流式调用当前后端，逐段产出文本。

    ``LLMBackend`` 只要求实现 ``chat``；自定义后端如果没有 ``chat_stream``，
    这里退化成一次性返回整段文本，调用方逻辑不需要区分。
    """
    backend = get_backend()
    streamer = getattr(backend, "chat_stream", None)
    if streamer is not None:
        async for piece in streamer(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        ):
            if piece:
                yield piece
        return

    text = await backend.chat(
        messages, model=model, temperature=temperature, max_tokens=max_tokens
    )
    if text:
        yield text
