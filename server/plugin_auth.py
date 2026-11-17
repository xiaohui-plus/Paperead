"""插件 token 与配对码管理。

插件只能访问 /api/ext/v1，主应用 token 不向扩展暴露。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

EXT_ORIGIN_RE = re.compile(r"^(?:chrome-extension|moz-extension)://[A-Za-z0-9_-]+$")
PAIR_TTL_SECONDS = 120
PAIR_MAX_FAILURES = 5
PAIR_FAILURE_WINDOW = 60


class PairingError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def is_extension_origin(origin: str) -> bool:
    return bool(origin and EXT_ORIGIN_RE.fullmatch(origin))


@dataclass
class Plugin:
    id: str
    name: str
    origin: str
    token_sha256: str
    scopes: list[str]
    created_at: float
    last_used_at: float


class PluginRegistry:
    """插件 token 的持久化注册表，只保存 SHA-256。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._plugins: list[Plugin] | None = None

    def _load(self) -> list[Plugin]:
        if self._plugins is None:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            rows = (payload.get("plugins") if isinstance(payload, dict) else None) or []
            self._plugins = [
                Plugin(
                    id=str(row.get("id") or ""),
                    name=str(row.get("name") or ""),
                    origin=str(row.get("origin") or ""),
                    token_sha256=str(row.get("token_sha256") or ""),
                    scopes=[str(item) for item in row.get("scopes") or []],
                    created_at=float(row.get("created_at") or 0),
                    last_used_at=float(row.get("last_used_at") or 0),
                )
                for row in rows
                if isinstance(row, dict) and row.get("id")
            ]
        return self._plugins

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "plugins": [asdict(plugin) for plugin in self._load()],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(self.path)

    def issue(self, origin: str, name: str) -> tuple[str, Plugin]:
        token = secrets.token_hex(32)
        now = time.time()
        plugin = Plugin(
            id=f"pl_{secrets.token_hex(6)}",
            name=(name.strip() or origin or "用户脚本")[:60],
            origin=origin,
            token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            scopes=[
                "library:read",
                "library:write",
                "reading:meta",
                "reading:translate",
                "reading:ask",
                "reading:annotate",
            ],
            created_at=now,
            last_used_at=now,
        )
        self._load().append(plugin)
        self._save()
        return token, plugin

    def verify(self, token: str, origin: str | None) -> Plugin | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        for plugin in self._load():
            if not hmac.compare_digest(plugin.token_sha256, digest):
                continue
            if origin and plugin.origin and plugin.origin != origin:
                return None
            if now - plugin.last_used_at > 60:
                plugin.last_used_at = now
                self._save()
            return plugin
        return None

    def list_public(self) -> list[dict]:
        return [
            {
                "id": plugin.id,
                "name": plugin.name,
                "origin": plugin.origin,
                "scopes": list(plugin.scopes),
                "created_at": plugin.created_at,
                "last_used_at": plugin.last_used_at,
            }
            for plugin in self._load()
        ]

    def revoke(self, plugin_id: str) -> bool:
        plugins = self._load()
        keep = [plugin for plugin in plugins if plugin.id != plugin_id]
        if len(keep) == len(plugins):
            return False
        self._plugins = keep
        self._save()
        return True


class PairingCodes:
    """内存中的一次性配对码与等待窗口，不落盘。

    两条路径共用同一把锁：
    - 手动码：桌面端生成 6 位数字，用户在扩展里输入。
    - 等待认领：桌面端点「连接浏览器插件」后开一个 120 秒窗口，
      扩展不带码调用即可直接绑定，用户不需要抄数字。
    """

    def __init__(self) -> None:
        self._code = ""
        self._expires_at = 0.0
        self._waiting_until = 0.0
        self._failures: dict[str, list[float]] = {}

    def issue(self) -> tuple[str, float]:
        self._code = f"{secrets.randbelow(1_000_000):06d}"
        self._expires_at = time.time() + PAIR_TTL_SECONDS
        # 同一时间只保留一种连接意图，避免用户看到两个入口
        self._waiting_until = 0.0
        return self._code, self._expires_at

    def open_wait(self) -> float:
        self._waiting_until = time.time() + PAIR_TTL_SECONDS
        self._code = ""
        self._expires_at = 0.0
        return self._waiting_until

    def consume(self, client: str, code: str) -> None:
        now = time.time()
        hits = [
            stamp
            for stamp in self._failures.get(client, [])
            if now - stamp < PAIR_FAILURE_WINDOW
        ]
        self._failures[client] = hits
        if len(hits) >= PAIR_MAX_FAILURES:
            raise PairingError(429, "尝试次数过多，请稍后再试")
        if not code.strip():
            if now > self._waiting_until or not self._waiting_until:
                raise PairingError(
                    410, "没有待连接的扩展，请先在 Paperead 中点击「连接浏览器插件」"
                )
            self._waiting_until = 0.0
            return
        if not self._code or now > self._expires_at:
            raise PairingError(410, "配对码已过期，请重新生成")
        if not hmac.compare_digest(code.strip(), self._code):
            hits.append(now)
            raise PairingError(401, "配对码不正确")
        self._code = ""
        self._expires_at = 0.0
        self._waiting_until = 0.0
