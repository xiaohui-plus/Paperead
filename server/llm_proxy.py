"""研究 agent 的本地 LLM 代理。

研究 agent（dsh）只会在启动时读取一次 PAPEREAD_LLM_BASE / PAPEREAD_LLM_KEY，
无法在运行中切换供应商。这里让 agent 固定访问本机代理，由前端把用户选中的
模型同步过来，代理再按选择注入真实的 baseURL、API Key 和模型名。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SSE_MEDIA = "text/event-stream"

ENV_BASE = os.environ.get("PAPEREAD_LLM_BASE", "https://api.deepseek.com").rstrip("/")
ENV_KEY = (
    os.environ.get("PAPEREAD_LLM_KEY")
    or os.environ.get("PAPEREAD_API_KEY")
    or os.environ.get("DEEPSEEK_API_KEY", "")
)
ENV_MODEL = os.environ.get("PAPEREAD_MODEL", "deepseek-v4-flash")

TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=120.0, pool=None)


class ResearchModelIn(BaseModel):
    base_url: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=8)
    model: str = Field(..., min_length=1)
    label: str = ""


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _shared_answer_models(
    data_dir: Path,
) -> tuple[str, list[dict[str, str]]]:
    """读取设置页保存的统一模型配置，返回当前选中的 id 与全部可用回答模型。"""
    try:
        payload = json.loads(
            (data_dir / "model-settings.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "", []
    settings = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings, dict):
        return "", []
    models = settings.get("models")
    if not isinstance(models, list):
        return "", []
    active = settings.get("active")
    active_id = str(active.get("answer") or "").strip() if isinstance(active, dict) else ""
    ready: list[dict[str, str]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") != "answer":
            continue
        base_url = str(item.get("baseUrl") or "").strip().rstrip("/")
        api_key = str(item.get("apiKey") or "").strip()
        model = str(item.get("model") or "").strip()
        if not (base_url and api_key and model):
            continue
        ready.append(
            {
                "id": str(item.get("id") or ""),
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "label": str(item.get("label") or ""),
            }
        )
    return active_id, ready


def _preferred_entry(
    entries: list[dict[str, str]], active_id: str
) -> dict[str, str] | None:
    for entry in entries:
        if entry["id"] and entry["id"] == active_id:
            return entry
    return entries[0] if entries else None


def _join(base: str, path: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/chat/completions") and path == "/chat/completions":
        return base
    return f"{base}{path}"


def _completion_to_sse(payload: dict[str, Any], model: str) -> bytes:
    """把一次性返回的 chat.completion 重新包装成 SSE 增量流。

    部分 OpenAI 兼容服务（自建网关、中转站）会忽略 `stream: true`，直接回一份
    完整 JSON。阅读里的问答走的是非流式调用，遇到这种服务照常能用；研究 agent
    只认 SSE，会一直读到结尾发现没有 `[DONE]`，于是整轮以 STREAM_CLOSED 失败。
    这里把同一份结果改写成 agent 能消费的流，两边行为就一致了。
    """
    choice = (payload.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    base = {
        "id": payload.get("id") or "chatcmpl-proxy",
        "object": "chat.completion.chunk",
        "created": payload.get("created") or int(time.time()),
        "model": payload.get("model") or model,
    }

    def emit(delta: dict[str, Any], finish: str | None = None) -> bytes:
        chunk = {
            **base,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}
            ],
        }
        return (
            "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        ).encode("utf-8")

    parts = [emit({"role": "assistant", "content": ""})]
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.append(emit({"reasoning_content": reasoning}))
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append(emit({"content": content}))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        parts.append(
            emit(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call.get("id") or f"call_{index}",
                            "type": "function",
                            "function": {
                                "name": (call.get("function") or {}).get("name", ""),
                                "arguments": (call.get("function") or {}).get(
                                    "arguments", ""
                                ),
                            },
                        }
                        for index, call in enumerate(tool_calls)
                        if isinstance(call, dict)
                    ]
                }
            )
        )
    finish = choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
    parts.append(emit({}, finish))
    parts.append(b"data: [DONE]\n\n")
    return b"".join(parts)


def build_llm_proxy_router(data_dir: Path, token: str) -> APIRouter:
    router = APIRouter()
    config_path = data_dir / "research-model.json"

    state: dict[str, str] = {
        "base_url": ENV_BASE,
        "api_key": ENV_KEY,
        "model": ENV_MODEL,
        "label": "",
        # env：还没人在研究页里选过模型，允许用设置页的配置兜底；
        # user：研究页明确选过，必须严格用它，否则会出现「界面显示 A、实际调用 B」。
        "source": "env",
    }

    def load() -> None:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        base_url = str(raw.get("base_url") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        model = str(raw.get("model") or "").strip()
        if base_url and api_key and model:
            state.update(
                base_url=base_url,
                api_key=api_key,
                model=model,
                label=str(raw.get("label") or ""),
                source="user",
            )

    def save() -> None:
        payload = {
            key: state[key] for key in ("base_url", "api_key", "model", "label")
        }
        try:
            config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            # 写盘失败不影响当前进程内使用，下次启动回退到环境变量
            pass

    load()

    def sync_shared() -> None:
        # 研究页明确选过的模型优先：设置页里可能还留着旧服务商/旧模型名，
        # 拿它覆盖会让 agent 调用一个和阅读问答完全不同的模型。
        if state["source"] == "user" and state["api_key"]:
            return
        active_id, entries = _shared_answer_models(data_dir)
        if not entries:
            return
        current = None
        if state["api_key"]:
            current = next(
                (
                    item
                    for item in entries
                    if item["base_url"] == state["base_url"]
                    and item["model"] == state["model"]
                ),
                None,
            )
        target = current or _preferred_entry(entries, active_id)
        if target is None:
            return
        picked = {
            key: target[key]
            for key in ("base_url", "api_key", "model", "label")
        }
        if all(state[key] == picked[key] for key in picked):
            return
        state.update(picked)
        save()

    sync_shared()

    def authorize(authorization: str | None) -> None:
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=403, detail="unauthorized")

    @router.get("/api/research/model")
    def get_model() -> dict[str, Any]:
        sync_shared()
        return {
            "base_url": state["base_url"],
            "api_key": _mask(state["api_key"]),
            "model": state["model"],
            "label": state["label"],
            "configured": bool(state["api_key"]),
        }

    @router.put("/api/research/model")
    def set_model(payload: ResearchModelIn) -> dict[str, Any]:
        state.update(
            base_url=payload.base_url.strip().rstrip("/"),
            api_key=payload.api_key.strip(),
            model=payload.model.strip(),
            label=payload.label.strip(),
            source="user",
        )
        save()
        return get_model()

    @router.post("/api/llm-proxy/v1/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        sync_shared()
        if not state["api_key"]:
            return JSONResponse(
                {
                    "error": {
                        "message": "尚未配置回答模型：请在设置里填写 API Key，或在研究输入框里选择模型。",
                        "type": "missing_credential",
                    }
                },
                status_code=503,
            )

        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid json body") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="invalid json body")
        body["model"] = state["model"]

        url = _join(state["base_url"], "/chat/completions")
        headers = {
            "authorization": f"Bearer {state['api_key']}",
            "content-type": "application/json",
            "accept": request.headers.get("accept", "text/event-stream"),
        }
        client = httpx.AsyncClient(timeout=TIMEOUT)
        upstream = None
        started = time.monotonic()
        try:
            upstream = await client.send(
                client.build_request("POST", url, json=body, headers=headers),
                stream=True,
            )
        except httpx.HTTPError as error:
            await client.aclose()
            reason = str(error) or type(error).__name__
            log.warning("llm 代理连接失败：url=%s model=%s %s", url, state["model"], reason)
            return JSONResponse(
                {
                    "error": {
                        "message": f"连接模型服务失败：{reason}",
                        "type": "transport",
                    }
                },
                status_code=502,
            )

        if upstream.status_code >= 400:
            detail = (await upstream.aread()).decode("utf-8", "replace")
            status = upstream.status_code
            await upstream.aclose()
            await client.aclose()
            log.warning(
                "llm 代理上游报错：url=%s model=%s status=%s body=%s",
                url,
                state["model"],
                status,
                detail[:500],
            )
            try:
                payload = json.loads(detail)
            except ValueError:
                payload = {"error": {"message": detail[:2000]}}
            return JSONResponse(payload, status_code=status)

        content_type = upstream.headers.get("content-type", "")
        chunks = upstream.aiter_raw()
        try:
            first = await chunks.__anext__()
        except StopAsyncIteration:
            first = b""
        except httpx.HTTPError as error:
            await upstream.aclose()
            await client.aclose()
            reason = str(error) or type(error).__name__
            log.warning("llm 代理读流失败：url=%s model=%s %s", url, state["model"], reason)
            return JSONResponse(
                {
                    "error": {
                        "message": f"读取模型响应失败：{reason}",
                        "type": "transport",
                    }
                },
                status_code=502,
            )
        status = upstream.status_code
        # 有些网关嘴上说 text/event-stream，实际吐的是 JSON，所以内容也要看一眼。
        looks_sse = SSE_MEDIA in content_type or first.lstrip().startswith(
            (b"data:", b":", b"event:")
        )
        if not looks_sse:
            # 供应商忽略了 stream:true（或这个地址根本不是 API）：自己包一层。
            # 否则 agent 会读到一段没有 [DONE] 的普通响应，并把整轮判成 STREAM_CLOSED。
            raw = first + b"".join([chunk async for chunk in chunks])
            await upstream.aclose()
            await client.aclose()
            log.warning(
                "llm 代理收到非流式响应：url=%s model=%s status=%s type=%s 耗时=%.2fs body=%s",
                url,
                state["model"],
                status,
                content_type or "-",
                time.monotonic() - started,
                raw[:300].decode("utf-8", "replace"),
            )
            if not raw.strip():
                return JSONResponse(
                    {
                        "error": {
                            "message": (
                                "模型服务返回了空响应（HTTP "
                                f"{status}，{content_type or '未知类型'}）："
                                "请确认模型名在该服务商下可用。"
                            ),
                            "type": "bad_gateway",
                        }
                    },
                    status_code=502,
                )
            try:
                payload: Any = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("choices"):
                return StreamingResponse(
                    iter([_completion_to_sse(payload, state["model"])]),
                    media_type=SSE_MEDIA,
                    headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
                )
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                return JSONResponse(payload, status_code=502)
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            "模型服务返回的不是 API 响应（HTTP "
                            f"{status}，{content_type or '未知类型'}）："
                            "请检查设置里的服务地址，应填该服务商的 API 地址，通常以 /v1 结尾。"
                            "返回内容开头：" + raw[:200].decode("utf-8", "replace")
                        ),
                        "type": "bad_gateway",
                    }
                },
                status_code=502,
            )

        log.info(
            "llm 代理：url=%s model=%s status=%s 建连=%.2fs",
            url,
            state["model"],
            status,
            time.monotonic() - started,
        )

        async def stream():
            try:
                if first:
                    yield first
                async for chunk in chunks:
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream(),
            status_code=status,
            media_type=SSE_MEDIA,
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return router
