"""研究 agent（Node DSH host）客户端。

端口与 token 只从环境变量读取：桌面壳在启动 Python 前注入，
前端永远看不到，也不会写进磁盘。
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx


class AgentUnavailable(RuntimeError):
    """agent 未启动、端口无效或连接中断。"""


def agent_target() -> tuple[str, str]:
    port = os.environ.get("PAPEREAD_AGENT_PORT")
    if not port:
        raise AgentUnavailable("研究 agent 尚未启动")
    try:
        number = int(port)
    except ValueError as error:
        raise AgentUnavailable(f"PAPEREAD_AGENT_PORT 无效：{port}") from error
    return f"http://127.0.0.1:{number}", os.environ.get("PAPEREAD_AGENT_TOKEN", "")


def available() -> bool:
    try:
        agent_target()
        return True
    except AgentUnavailable:
        return False


def _headers(token: str) -> dict[str, str]:
    return {"x-agent-token": token} if token else {}


async def post_json(
    path: str, payload: dict[str, Any], *, timeout: float = 60
) -> dict[str, Any]:
    base, token = agent_target()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base}{path}", json=payload, headers=_headers(token)
            )
    except httpx.HTTPError as error:
        raise AgentUnavailable(f"研究 agent 不可用：{error}") from error
    if response.status_code >= 400:
        raise AgentUnavailable(f"研究 agent 返回 {response.status_code}")
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def ensure_session(session_id: str, cwd: str) -> None:
    await post_json("/v1/sessions", {"sessionId": session_id, "cwd": cwd}, timeout=30)


async def send_message(session_id: str, text: str, cwd: str) -> None:
    await post_json(
        f"/v1/sessions/{session_id}/messages",
        {"text": text, "cwd": cwd},
        timeout=30,
    )


async def cancel(session_id: str) -> None:
    await post_json(f"/v1/sessions/{session_id}/cancel", {}, timeout=15)


async def stream_events(session_id: str, after: int) -> AsyncIterator[dict[str, Any]]:
    """订阅 agent SSE，逐条产出事件字典；连接异常统一抛 AgentUnavailable。"""
    base, token = agent_target()
    client = httpx.AsyncClient(timeout=None)
    try:
        request = client.build_request(
            "GET",
            f"{base}/v1/sessions/{session_id}/events",
            params={"after": max(0, int(after))},
            headers=_headers(token),
        )
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            raise AgentUnavailable(f"研究 agent 返回 {response.status_code}")
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event
    except httpx.HTTPError as error:
        raise AgentUnavailable(f"研究 agent 不可用：{error}") from error
    finally:
        await client.aclose()
