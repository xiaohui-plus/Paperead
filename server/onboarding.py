"""首次启动引导状态。

桌面客户端第一次启动时由前端询问用户「开始教程 / 跳过」，这个选择只需要
记录一次。状态放在应用数据目录，和文献库、研究库同级；版本更新不会重置，
只有用户主动清空应用数据才会重新出现。

网页端不会调用这组接口，是否弹出由前端根据 Tauri 环境判断。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

STATE_NAME = "onboarding.json"
STATE_VERSION = 1

VALID_ACTIONS = {
    "shown",
    "start",
    "skip",
    "legacy",
    "progress",
    "dismiss",
    "complete",
}


def _now() -> int:
    return int(time.time())


class OnboardingStore:
    """onboarding.json 的读写；写入用临时文件替换，避免半截文件。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / STATE_NAME
        self._lock = Lock()

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self.read()
            state.update(patch)
            state["version"] = STATE_VERSION
            state["updatedAt"] = _now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.tmp")
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)
            return state

    def snapshot(self, legacy_user: bool = False) -> dict[str, Any]:
        state = self.read()
        prompted = bool(state.get("promptedAt"))
        return {
            "prompted": prompted,
            "promptedAt": int(state.get("promptedAt") or 0),
            "choice": state.get("choice"),
            "status": state.get("status") or "idle",
            "stepId": state.get("stepId"),
            "legacyUser": bool(legacy_user or state.get("choice") == "legacy"),
            "shouldPrompt": not prompted and not legacy_user,
            "dismissed": bool(state.get("dismissed", False)),
        }


class OnboardingAction(BaseModel):
    action: str
    stepId: str | None = None


def build_onboarding_router(
    store: OnboardingStore,
    has_user_data: Callable[[], bool],
) -> APIRouter:
    router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

    @router.get("")
    def get_state() -> dict[str, Any]:
        # 老版本升级上来的用户已经有文献或项目，不应再看到教程弹窗。
        legacy = bool(has_user_data()) and not store.path.exists()
        return store.snapshot(legacy_user=legacy)

    @router.post("")
    def update_state(body: OnboardingAction) -> dict[str, Any]:
        action = (body.action or "").strip()
        if action not in VALID_ACTIONS:
            raise HTTPException(status_code=400, detail="unknown onboarding action")
        if action == "progress" and not body.stepId:
            raise HTTPException(status_code=400, detail="stepId is required")

        current = store.read()
        prompted_at = int(current.get("promptedAt") or _now())
        patch: dict[str, Any] = {"promptedAt": prompted_at}

        if action == "shown":
            patch.update({"status": "prompted", "choice": current.get("choice")})
        elif action == "start":
            patch.update(
                {
                    "choice": "start",
                    "status": "in_progress",
                    "stepId": body.stepId,
                }
            )
        elif action == "skip":
            patch.update(
                {
                    "choice": "skip",
                    "status": "skipped",
                    "stepId": None,
                    "dismissed": True,
                }
            )
        elif action == "legacy":
            patch.update(
                {
                    "choice": "legacy",
                    "status": "skipped",
                    "stepId": None,
                }
            )
        elif action == "progress":
            patch.update(
                {
                    "choice": current.get("choice") or "start",
                    "status": "in_progress",
                    "stepId": body.stepId,
                }
            )
        elif action == "dismiss":
            # 关闭提醒：保留当前步骤，但设置里不再显示新手教程入口
            patch.update(
                {
                    "choice": current.get("choice") or "start",
                    "status": "in_progress",
                    "stepId": current.get("stepId"),
                    "dismissed": True,
                }
            )
        else:  # complete
            patch.update({"status": "completed", "stepId": None})

        store.write(patch)
        return store.snapshot(legacy_user=False)

    return router
