"""研究 agent 的本地 LLM 代理。

研究 agent（dsh）只会在启动时读取一次 PAPEREAD_LLM_BASE / PAPEREAD_LLM_KEY，
无法在运行中切换供应商。这里让 agent 固定访问本机代理，由前端把用户选中的
模型同步过来，代理再按选择注入真实的 baseURL、API Key 和模型名。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

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


def build_llm_proxy_router(data_dir: Path, token: str) -> APIRouter:
    router = APIRouter()
    config_path = data_dir / "research-model.json"

    state: dict[str, str] = {
        "base_url": ENV_BASE,
        "api_key": ENV_KEY,
        "model": ENV_MODEL,
        "label": "",
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
            )

    def save() -> None:
        try:
            config_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            # 写盘失败不影响当前进程内使用，下次启动回退到环境变量
            pass

    load()

    def sync_shared() -> None:
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
        try:
            upstream = await client.send(
                client.build_request("POST", url, json=body, headers=headers),
                stream=True,
            )
        except httpx.HTTPError as error:
            await client.aclose()
            reason = str(error) or type(error).__name__
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
            await upstream.aclose()
            await client.aclose()
            try:
                payload = json.loads(detail)
            except ValueError:
                payload = {"error": {"message": detail[:2000]}}
            return JSONResponse(payload, status_code=upstream.status_code)

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return router
