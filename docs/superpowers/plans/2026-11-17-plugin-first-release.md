# Paperead 浏览器插件第一版实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在 arXiv、PubMed、PMC 和常见出版商页面一键把论文与 PDF 存入本地 Paperead，并在现有阅读页打开。

**Architecture:** 桌面端新增受控的 `/api/ext/v1` 插件命名空间，扩展通过一次性配对码换取作用域 token；扩展只负责提取元数据、获取 PDF 字节和反馈状态，解析、翻译、问答和阅读全部复用现有服务与阅读页。

**Tech Stack:** Python 3.10+、FastAPI、pytest、React 19、TypeScript、Vite、WXT、Vitest、jsdom

**Spec:** `docs/superpowers/specs/2026-11-17-browser-extension-plugin-design.md`

---

## 范围拆分

本计划包含两个可独立验收的阶段：

- **P0（Task 1–5）**：服务端插件 API、配对、设置页面板。完成后可用 curl 走通全链路，不依赖浏览器扩展。
- **P1（Task 6–11）**：Chrome/Edge 扩展。完成后可在 arXiv/PubMed 一键入库。

Firefox、Scholar 批量、侧栏问答不在本计划内。

## 文件地图

| 文件 | 职责 |
|---|---|
| `server/plugin_auth.py` | 插件 token 注册表、配对码、Origin 判定 |
| `server/ext_api.py` | `/api/ext/v1` 路由与请求校验 |
| `server/main.py` | 挂载路由、扩展 Origin/CORS、鉴权分支 |
| `tests/test_plugin_auth.py` | token 与配对码单元测试 |
| `tests/test_ext_api.py` | 插件 API 集成测试 |
| `web/src/lib/plugins.ts` | 设置页调用配对与管理接口 |
| `web/src/components/PluginSettingsPanel.tsx` | 「插件与连接」面板 |
| `web/src/components/SettingsDialog.tsx` | 新增插件页签 |
| `web/src/components/icons.tsx` | 插件图标 |
| `web/src/styles.css` | 插件面板样式 |
| `extensions/browser/` | WXT + React 扩展 |
| `extensions/browser/lib/metadata.ts` | 页面元数据提取（可注入、可测试） |
| `extensions/browser/lib/api.ts` | 本地 API 客户端与 token |
| `extensions/browser/lib/pdf.ts` | PDF 获取与魔数校验 |
| `extensions/browser/entrypoints/background.ts` | 保存任务、轮询、通知、右键菜单 |
| `extensions/browser/entrypoints/popup/` | 弹窗 UI |

## 实施前注意

仓库当前已有未提交改动（`server/research/*`、`web/src/research/*`）。本计划的所有 `git add` 都必须按文件名精确添加，禁止 `git add -A`。

---

## Task 1: 插件 token 与配对码

**Files:**
- Create: `server/plugin_auth.py`
- Test: `tests/test_plugin_auth.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_plugin_auth.py`：

```python
import time

import pytest

from server.plugin_auth import (
    PairingCodes,
    PairingError,
    PluginRegistry,
    is_extension_origin,
)


def test_extension_origin_detection():
    assert is_extension_origin("chrome-extension://abcdefghijklmnop")
    assert is_extension_origin("moz-extension://6f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f")
    assert not is_extension_origin("https://arxiv.org")
    assert not is_extension_origin("")


def test_registry_issue_verify_and_revoke(tmp_path):
    registry = PluginRegistry(tmp_path / "plugins.json")
    token, plugin = registry.issue("chrome-extension://abc", "Chrome")

    assert registry.verify(token, "chrome-extension://abc") is not None
    assert registry.verify(token, "chrome-extension://other") is None
    assert registry.verify("wrong-token", "chrome-extension://abc") is None

    assert registry.revoke(plugin.id) is True
    assert registry.verify(token, "chrome-extension://abc") is None
    assert registry.revoke(plugin.id) is False


def test_registry_persists_without_plain_token(tmp_path):
    path = tmp_path / "plugins.json"
    registry = PluginRegistry(path)
    token, _ = registry.issue("chrome-extension://abc", "Chrome")

    text = path.read_text(encoding="utf-8")
    assert token not in text
    assert "token_sha256" in text

    reloaded = PluginRegistry(path)
    assert reloaded.verify(token, "chrome-extension://abc") is not None


def test_pairing_code_is_single_use():
    codes = PairingCodes()
    code, expires_at = codes.issue()
    assert len(code) == 6
    assert expires_at > time.time()

    codes.consume("127.0.0.1", code)

    with pytest.raises(PairingError) as error:
        codes.consume("127.0.0.1", code)
    assert error.value.status_code == 410


def test_pairing_code_rejects_wrong_code():
    codes = PairingCodes()
    codes.issue()

    with pytest.raises(PairingError) as error:
        codes.consume("127.0.0.1", "000000")
    assert error.value.status_code == 401


def test_pairing_code_rate_limits_failures():
    codes = PairingCodes()
    codes.issue()

    for _ in range(5):
        with pytest.raises(PairingError):
            codes.consume("127.0.0.1", "000000")

    with pytest.raises(PairingError) as error:
        codes.consume("127.0.0.1", "000000")
    assert error.value.status_code == 429
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```powershell
.venv-dev\Scripts\python.exe -m pytest tests/test_plugin_auth.py -v
```

Expected: FAIL，`ModuleNotFoundError: No module named 'server.plugin_auth'`。

- [ ] **Step 3: 实现 `server/plugin_auth.py`**

```python
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
            rows = payload.get("plugins") if isinstance(payload, dict) else []
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
            name=(name.strip() or origin)[:60],
            origin=origin,
            token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            scopes=["library:read", "library:write"],
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
            if origin and plugin.origin != origin:
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
    """内存中的一次性配对码，不落盘。"""

    def __init__(self) -> None:
        self._code = ""
        self._expires_at = 0.0
        self._failures: dict[str, list[float]] = {}

    def issue(self) -> tuple[str, float]:
        self._code = f"{secrets.randbelow(1_000_000):06d}"
        self._expires_at = time.time() + PAIR_TTL_SECONDS
        return self._code, self._expires_at

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
        if not self._code or now > self._expires_at:
            raise PairingError(410, "配对码已过期，请重新生成")
        if not hmac.compare_digest(code.strip(), self._code):
            hits.append(now)
            raise PairingError(401, "配对码不正确")
        self._code = ""
        self._expires_at = 0.0
```

- [ ] **Step 4: 运行测试确认通过**

Run:

```powershell
.venv-dev\Scripts\python.exe -m pytest tests/test_plugin_auth.py -v
```

Expected: 6 passed。

- [ ] **Step 5: 提交**

```powershell
git add server/plugin_auth.py tests/test_plugin_auth.py
git commit -m "feat: add plugin token registry and pairing codes"
```

---

## Task 2: 插件 API 路由

**Files:**
- Create: `server/ext_api.py`
- Test: `tests/test_ext_api.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_ext_api.py`：

```python
import importlib
import json

import fitz
from fastapi.testclient import TestClient

ORIGIN = "chrome-extension://abcdefghijklmnop"


def _pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Paperead plugin test")
    data = doc.tobytes()
    doc.close()
    return data


def _client(monkeypatch, tmp_path, token: str = ""):
    data = tmp_path / "lib"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAPEREAD_DATA", str(data))
    monkeypatch.setenv("PAPEREAD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    if token:
        monkeypatch.setenv("PAPEREAD_TOKEN", token)
    else:
        monkeypatch.delenv("PAPEREAD_TOKEN", raising=False)

    import server.main as main

    importlib.reload(main)

    async def noop(paper_id: str) -> None:
        return None

    monkeypatch.setattr(main, "_parse_task", noop)
    return main, TestClient(main.app)


def _pair(client: TestClient) -> str:
    session = client.post("/api/ext/v1/pair/session", json={"name": "Chrome"})
    assert session.status_code == 200, session.text
    code = session.json()["code"]
    paired = client.post(
        "/api/ext/v1/pair",
        json={"code": code, "name": "Chrome"},
        headers={"Origin": ORIGIN},
    )
    assert paired.status_code == 200, paired.text
    return paired.json()["token"]


def test_ping_is_public(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path, token="test-token")
    with client:
        response = client.get("/api/ext/v1/ping")
        assert response.status_code == 200
        assert response.json()["app"] == "paperead"


def test_pair_requires_extension_origin(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        code = client.post("/api/ext/v1/pair/session", json={"name": "Chrome"}).json()[
            "code"
        ]
        denied = client.post(
            "/api/ext/v1/pair",
            json={"code": code, "name": "Chrome"},
            headers={"Origin": "https://evil.example"},
        )
        assert denied.status_code == 403


def test_ingest_lookup_and_dedupe(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        metadata = {
            "source": "arxiv",
            "source_id": "arxiv:2401.00001",
            "title": "A Test Paper",
            "authors": "Ada Lovelace, Alan Turing",
            "abstract": "Testing ingestion.",
            "doi": "10.1000/test",
            "url": "https://arxiv.org/abs/2401.00001",
            "year": 2024,
            "venue": "TestConf",
        }
        files = {"file": ("paper.pdf", _pdf_bytes(), "application/pdf")}
        first = client.post(
            "/api/ext/v1/papers/ingest",
            files=files,
            data={"metadata": json.dumps(metadata)},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["already"] is False
        assert body["paper"]["title"] == "A Test Paper"
        assert body["paper"]["year"] == 2024
        assert body["paper"]["venue"] == "TestConf"
        paper_id = body["paper"]["id"]

        second = client.post(
            "/api/ext/v1/papers/ingest",
            files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
            data={"metadata": json.dumps(metadata)},
            headers=headers,
        )
        assert second.status_code == 200
        assert second.json()["already"] is True
        assert second.json()["paper"]["id"] == paper_id

        found = client.get(
            "/api/ext/v1/papers/lookup",
            params={"source_id": "arxiv:2401.00001"},
            headers=headers,
        )
        assert found.status_code == 200
        assert found.json()["found"] is True
        assert found.json()["paper"]["id"] == paper_id

        status = client.get(f"/api/ext/v1/papers/{paper_id}", headers=headers)
        assert status.status_code == 200
        assert status.json()["paper"]["status"] in {"uploaded", "parsing", "ready"}


def test_ingest_rejects_non_pdf(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        response = client.post(
            "/api/ext/v1/papers/ingest",
            files={"file": ("paper.pdf", b"not a pdf", "application/pdf")},
            data={"metadata": "{}"},
            headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
        )
        assert response.status_code == 400


def test_plugin_token_is_revoked(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        token = _pair(client)
        headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        plugins = client.get("/api/ext/v1/admin/plugins").json()["plugins"]
        assert len(plugins) == 1

        deleted = client.delete(f"/api/ext/v1/admin/plugins/{plugins[0]['id']}")
        assert deleted.status_code == 200

        denied = client.get("/api/ext/v1/folders", headers=headers)
        assert denied.status_code == 401
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```powershell
.venv-dev\Scripts\python.exe -m pytest tests/test_ext_api.py -v
```

Expected: FAIL，`ModuleNotFoundError: No module named 'server.ext_api'` 或 404。

- [ ] **Step 3: 实现 `server/ext_api.py`**

```python
"""浏览器插件接口：/api/ext/v1。

只做采集入库，不做阅读。主应用鉴权仍由 server.main.guard 负责；
本模块校验插件 token 与扩展 Origin。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from paperead import __version__
from server.plugin_auth import PairingCodes, PairingError, Plugin, PluginRegistry, is_extension_origin

MAX_PDF_BYTES = 100 * 1024 * 1024
META_FIELDS = (
    "source",
    "source_id",
    "title",
    "authors",
    "abstract",
    "doi",
    "url",
    "venue",
)


class PairIn(BaseModel):
    code: str = Field(..., min_length=4, max_length=12)
    name: str = ""


class PairSessionIn(BaseModel):
    name: str = ""


def _open_url(request: Request, paper_id: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/?p={paper_id}"


def _normalize_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip()
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _clean_meta(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("metadata 必须是对象")
    meta: dict[str, Any] = {}
    for key in META_FIELDS:
        value = str(raw.get(key) or "").strip()
        if value:
            meta[key] = value
    year = raw.get("year")
    if year not in (None, ""):
        try:
            meta["year"] = int(year)
        except (TypeError, ValueError):
            pass
    return meta


def _safe_filename(name: str, paper_id: str) -> str:
    stem = Path(name or "paper").stem
    stem = re.sub(r"[^\w.\- ]+", "_", stem).strip(" .")[:80]
    return f"{stem or paper_id}.pdf"


def build_router(
    *,
    store: Any,
    registry: PluginRegistry,
    parse_task: Callable[[str], Awaitable[None]],
    public: Callable[[dict[str, Any]], dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/api/ext/v1", tags=["ext"])
    codes = PairingCodes()

    def require_plugin(
        request: Request, authorization: str | None = Header(default=None)
    ) -> Plugin:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少插件 token")
        token = authorization.removeprefix("Bearer ").strip()
        plugin = registry.verify(token, request.headers.get("origin"))
        if plugin is None:
            raise HTTPException(status_code=401, detail="插件 token 无效或已撤销")
        return plugin

    @router.get("/ping")
    async def ping() -> dict:
        return {"ok": True, "app": "paperead", "api": 1, "version": __version__}

    @router.post("/pair/session")
    async def pair_session(payload: PairSessionIn) -> dict:
        code, expires_at = codes.issue()
        return {"code": code, "expires_at": expires_at}

    @router.post("/pair")
    async def pair(request: Request, payload: PairIn) -> dict:
        origin = request.headers.get("origin", "")
        if not is_extension_origin(origin):
            raise HTTPException(status_code=403, detail="只允许浏览器扩展配对")
        client = request.client.host if request.client else "unknown"
        try:
            codes.consume(client, payload.code)
        except PairingError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        token, plugin = registry.issue(origin, payload.name)
        return {"token": token, "api": 1, "scopes": plugin.scopes}

    @router.get("/papers/lookup")
    async def lookup(
        request: Request,
        source_id: str = Query(""),
        doi: str = Query(""),
        url: str = Query(""),
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_plugin(request, authorization)
        row = store.find_by_source(source_id) if source_id else None
        if row is None and doi:
            needle = doi.strip().lower()
            row = next(
                (item for item in store.list() if str(item.get("doi") or "").lower() == needle),
                None,
            )
        if row is None and url:
            needle = _normalize_url(url)
            row = next(
                (
                    item
                    for item in store.list()
                    if item.get("url") and _normalize_url(str(item["url"])) == needle
                ),
                None,
            )
        if row is None:
            return {"found": False, "paper": None}
        return {
            "found": True,
            "paper": {**public(row), "open_url": _open_url(request, row["id"])},
        }

    @router.post("/papers/ingest")
    async def ingest(
        request: Request,
        file: UploadFile = File(...),
        metadata: str = Form("{}"),
        folder_id: str = Form(""),
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_plugin(request, authorization)
        data = await file.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="PDF 超过 100MB")
        if len(data) < 1024 or not data.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="不是有效的 PDF")
        if folder_id and store.get_folder(folder_id) is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        try:
            meta = _clean_meta(json.loads(metadata or "{}"))
        except (ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if folder_id:
            meta["folder_id"] = folder_id

        existing = store.find_by_source(meta.get("source_id", ""))
        if existing is None:
            existing = store.find_by_pdf_hash(data, folder_id=folder_id or None)
        if existing is not None:
            if existing.get("status") != "ready":
                asyncio.create_task(parse_task(existing["id"]))
            return {
                "paper": {**public(existing), "open_url": _open_url(request, existing["id"])},
                "already": True,
            }

        filename = _safe_filename(file.filename or "paper.pdf", "paper")
        row = store.add(filename, data, meta or None)
        asyncio.create_task(parse_task(row["id"]))
        return {
            "paper": {**public(row), "open_url": _open_url(request, row["id"])},
            "already": False,
        }

    @router.get("/papers/{paper_id}")
    async def paper_status(
        request: Request,
        paper_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_plugin(request, authorization)
        row = store.get(paper_id)
        if row is None or row.get("project_id"):
            raise HTTPException(status_code=404, detail="论文不存在")
        return {
            "paper": public(row),
            "open_url": _open_url(request, paper_id),
        }

    @router.get("/folders")
    async def folders(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_plugin(request, authorization)
        return {
            "folders": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "count": item.get("paper_count", 0),
                }
                for item in store.list_folders()
            ]
        }

    @router.get("/admin/plugins")
    async def admin_plugins() -> dict:
        return {"plugins": registry.list_public()}

    @router.delete("/admin/plugins/{plugin_id}")
    async def admin_revoke(plugin_id: str) -> dict:
        if not registry.revoke(plugin_id):
            raise HTTPException(status_code=404, detail="插件不存在")
        return {"ok": True}

    return router
```

- [ ] **Step 4: 运行测试**

Run:

```powershell
.venv-dev\Scripts\python.exe -m pytest tests/test_ext_api.py -v
```

Expected: 5 passed。若出现 404，说明路由尚未挂载，进入 Task 3。

- [ ] **Step 5: 提交**

```powershell
git add server/ext_api.py tests/test_ext_api.py
git commit -m "feat: add extension ingestion API"
```

---

## Task 3: 挂载路由并改造鉴权

**Files:**
- Modify: `server/main.py`
- Test: `tests/test_ext_api.py`

- [ ] **Step 1: 增加失败用例**

在 `tests/test_ext_api.py` 末尾追加：

```python
def test_plugin_endpoint_requires_token(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    with client:
        response = client.get("/api/ext/v1/folders")
        assert response.status_code == 401


def test_main_api_still_requires_main_token(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path, token="main-token")
    with client:
        assert client.get("/api/papers").status_code == 403
        assert (
            client.get("/api/papers", headers={"X-Paperead-Token": "main-token"}).status_code
            == 200
        )
```

- [ ] **Step 2: 运行确认失败**

Run:

```powershell
.venv-dev\Scripts\python.exe -m pytest tests/test_ext_api.py -v
```

Expected: 前两个新用例中至少 `test_plugin_endpoint_requires_token` 失败（路由 404 或鉴权未生效）。

- [ ] **Step 3: 在 `server/main.py` 挂载路由**

在 import 区加入：

```python
from server.ext_api import build_router as build_ext_router
from server.plugin_auth import PluginRegistry, is_extension_origin
from server.runtime import (
    allowed_origins,
    bind_host,
    cleanup_previous_data_dir,
    config_dir,
    default_data_dir,
    ensure_data_dir,
    is_frozen,
    load_data_location,
    save_data_location,
    web_dist,
)
```

注意：`config_dir` 需要合并进已有的 `from server.runtime import (...)`，不要重复 import 块。

在 `store = LibraryStore(DATA_DIR)` 之后加入：

```python
plugin_registry = PluginRegistry(config_dir() / "plugins.json")
app.include_router(
    build_ext_router(
        store=store,
        registry=plugin_registry,
        parse_task=lambda paper_id: _parse_task(paper_id),
        public=lambda row: _public(row),
    )
)
```

`_parse_task` 和 `_public` 在文件后面才定义，这里传 lambda 是为了延迟解析，不要在模块顶部直接引用函数对象。

- [ ] **Step 4: 改造 `guard`**

把现有 `guard` 中 `/api` 的 token 读取抽成模块级函数：

```python
def _supplied_token(request: Request) -> str:
    return (
        request.cookies.get("pr_token")
        or request.headers.get("x-paperead-token")
        or request.headers.get("x-paper-reflow-token")
        or request.query_params.get("pr_token")
        or ""
    )
```

把 `guard` 改为：

```python
@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    origin = request.headers.get("origin", "")
    ext_path = path.startswith("/api/ext/v1")

    if ext_path and request.method == "OPTIONS":
        if is_extension_origin(origin):
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Headers": "authorization, content-type",
                    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                    "Access-Control-Max-Age": "600",
                },
            )
        return Response(status_code=403)

    if path.startswith("/api/llm-proxy/"):
        if TOKEN and request.headers.get("authorization") != f"Bearer {TOKEN}":
            return JSONResponse({"detail": "unauthorized"}, status_code=403)
    elif ext_path:
        if path in {"/api/ext/v1/ping", "/api/ext/v1/pair"}:
            if path.endswith("/pair") and not is_extension_origin(origin):
                return JSONResponse(
                    {"detail": "只允许浏览器扩展配对"}, status_code=403
                )
        elif path == "/api/ext/v1/pair/session" or path.startswith(
            "/api/ext/v1/admin/"
        ):
            if TOKEN and _supplied_token(request) != TOKEN:
                return JSONResponse({"detail": "unauthorized"}, status_code=403)
            if (
                origin
                and not is_extension_origin(origin)
                and origin not in ORIGINS
                and urlparse(origin).netloc != request.headers.get("host", "")
            ):
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        else:
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            if (
                origin
                and not is_extension_origin(origin)
                and origin not in ORIGINS
                and urlparse(origin).netloc != request.headers.get("host", "")
            ):
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    elif path.startswith("/api"):
        if TOKEN and _supplied_token(request) != TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=403)
        if (
            origin
            and origin not in ORIGINS
            and urlparse(origin).netloc != request.headers.get("host", "")
        ):
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)

    response = await call_next(request)
    if ext_path and is_extension_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"

    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        client_host = request.client.host if request.client else ""
        local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
        if TOKEN and local and "pr_token" not in request.cookies:
            response.set_cookie(
                "pr_token", TOKEN, httponly=True, samesite="strict", path="/"
            )
    return response
```

关键点：

- `/api/ext/v1/ping` 和 `/api/ext/v1/pair` 是唯一豁免主 token 的路径。
- `/api/ext/v1/pair/session` 和 `/admin/*` 继续走主 token。
- 插件业务接口只检查 `Authorization: Bearer` 是否存在，真正的 token 校验在路由内完成。
- 主 `/api` 的行为保持不变。

- [ ] **Step 5: 放开扩展 Origin 的 CORS 预检**

把现有的 `app.add_middleware(CORSMiddleware, ...)` 改为：

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ORIGINS),
    allow_origin_regex=r"^(chrome-extension|moz-extension)://[A-Za-z0-9_-]+$",
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 6: 运行测试**

Run:

```powershell
.venv-dev\Scripts\python.exe -m pytest tests/test_ext_api.py tests/test_server_security.py -v
```

Expected: 全部通过。`test_server_security.py` 用来确认主 `/api` 没有被改坏。

- [ ] **Step 7: 提交**

```powershell
git add server/main.py tests/test_ext_api.py
git commit -m "feat: mount extension API and allow extension origins"
```

---

## Task 4: 设置页接口客户端

**Files:**
- Create: `web/src/lib/plugins.ts`

- [ ] **Step 1: 实现客户端**

```ts
import { apiFetch } from './mobile'

export interface PluginInfo {
  id: string
  name: string
  origin: string
  scopes: string[]
  created_at: number
  last_used_at: number
}

export interface PairingCode {
  code: string
  expires_at: number
}

async function readJson<T>(response: Response): Promise<T> {
  const data = (await response.json().catch(() => ({}))) as {
    detail?: string
  }
  if (!response.ok) {
    throw new Error(data.detail || `请求失败（HTTP ${response.status}）`)
  }
  return data as T
}

export async function createPairingCode(name = '浏览器扩展'): Promise<PairingCode> {
  const response = await apiFetch('/api/ext/v1/pair/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  return readJson<PairingCode>(response)
}

export async function listPlugins(): Promise<PluginInfo[]> {
  const response = await apiFetch('/api/ext/v1/admin/plugins')
  const data = await readJson<{ plugins: PluginInfo[] }>(response)
  return data.plugins
}

export async function revokePlugin(id: string): Promise<void> {
  const response = await apiFetch(
    `/api/ext/v1/admin/plugins/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
  )
  await readJson<{ ok: boolean }>(response)
}
```

- [ ] **Step 2: 类型检查**

Run:

```powershell
npm --prefix web run build
```

Expected: 构建通过。此时面板还没接入，不会影响现有页面。

- [ ] **Step 3: 提交**

```powershell
git add web/src/lib/plugins.ts
git commit -m "feat: add plugin settings API client"
```

---

## Task 5: 「插件与连接」设置面板

**Files:**
- Create: `web/src/components/PluginSettingsPanel.tsx`
- Modify: `web/src/components/SettingsDialog.tsx`
- Modify: `web/src/components/icons.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: 添加图标**

在 `web/src/components/icons.tsx` 末尾加入：

```tsx
export function IconPlug() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
    >
      <path d="M9 3v6M15 3v6M7 9h10v3a5 5 0 0 1-10 0V9Z" />
      <path d="M12 17v4" />
    </svg>
  )
}
```

- [ ] **Step 2: 实现面板**

创建 `web/src/components/PluginSettingsPanel.tsx`：

```tsx
import { useCallback, useEffect, useState } from 'react'
import {
  createPairingCode,
  listPlugins,
  revokePlugin,
  type PairingCode,
  type PluginInfo,
} from '../lib/plugins'

function formatTime(value: number) {
  if (!value) return '—'
  return new Date(value * 1000).toLocaleString()
}

export function PluginSettingsPanel() {
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [pairing, setPairing] = useState<PairingCode | null>(null)
  const [secondsLeft, setSecondsLeft] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      setPlugins(await listPlugins())
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取插件列表失败')
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    if (!pairing) return
    const timer = window.setInterval(() => {
      const left = Math.max(0, Math.ceil(pairing.expires_at - Date.now() / 1000))
      setSecondsLeft(left)
      if (left === 0) {
        window.clearInterval(timer)
        setPairing(null)
      }
    }, 1000)
    setSecondsLeft(Math.max(0, Math.ceil(pairing.expires_at - Date.now() / 1000)))
    return () => window.clearInterval(timer)
  }, [pairing])

  const generate = async () => {
    setLoading(true)
    try {
      const code = await createPairingCode()
      setPairing(code)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成配对码失败')
    } finally {
      setLoading(false)
    }
  }

  const remove = async (plugin: PluginInfo) => {
    if (!window.confirm(`撤销「${plugin.name}」的访问权限？`)) return
    try {
      await revokePlugin(plugin.id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '撤销失败')
    }
  }

  return (
    <div className="modal-body plugin-settings">
      <section className="model-section">
        <div className="section-head">
          <h3>浏览器插件</h3>
          <span>把网页上的论文送进本地文献库</span>
        </div>
        <p className="model-empty">
          Paperead Connector 只读取当前网页的论文信息和 PDF，并通过本机接口存入文献库。
          插件不能访问模型配置、研究项目或文件系统。
        </p>
        <div className="plugin-install-links">
          <span>安装：</span>
          <a href="https://github.com/xiaohui-plus/Paperead" target="_blank" rel="noreferrer">
            Chrome / Edge 构建说明 ↗
          </a>
        </div>
      </section>

      <section className="model-section">
        <div className="section-head">
          <h3>连接新插件</h3>
          <span>配对码 120 秒内有效</span>
        </div>
        {pairing ? (
          <div className="plugin-pair-box">
            <b className="plugin-pair-code">{pairing.code}</b>
            <span>在插件弹窗中输入这 6 位数字，剩余 {secondsLeft} 秒</span>
          </div>
        ) : (
          <button className="btn primary" onClick={() => void generate()} disabled={loading}>
            {loading ? '生成中…' : '生成配对码'}
          </button>
        )}
        {error && <p className="plugin-error">{error}</p>}
      </section>

      <section className="model-section">
        <div className="section-head">
          <h3>已连接的插件</h3>
          <span>{plugins.length} 个</span>
        </div>
        {plugins.length === 0 ? (
          <p className="model-empty">还没有插件连接。生成配对码后，在扩展弹窗里完成连接。</p>
        ) : (
          <div className="plugin-list">
            {plugins.map((plugin) => (
              <article className="plugin-card" key={plugin.id}>
                <div>
                  <b>{plugin.name}</b>
                  <span>{plugin.origin}</span>
                  <span>最近使用：{formatTime(plugin.last_used_at)}</span>
                </div>
                <button className="link-btn danger" onClick={() => void remove(plugin)}>
                  撤销
                </button>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
```

- [ ] **Step 3: 接入设置页**

在 `web/src/components/SettingsDialog.tsx`：

1. import：

```tsx
import { PluginSettingsPanel } from './PluginSettingsPanel'
```

2. 在 icons import 中加入 `IconPlug`。

3. 把 tab 类型改为：

```tsx
const [tab, setTab] = useState<
  | 'models'
  | 'reading'
  | 'scholar'
  | 'review'
  | 'sync'
  | 'plugins'
  | 'general'
  | 'feedback'
>('models')
```

4. 在「文件同步」按钮之后加入（手机端不显示）：

```tsx
{!mobile && (
  <button
    className="library-nav-item"
    data-active={tab === 'plugins'}
    onClick={() => setTab('plugins')}
  >
    <IconPlug />
    <span>插件与连接</span>
  </button>
)}
```

5. 把现有的移动端回退 effect 改成同时处理插件页：

```tsx
useEffect(() => {
  if (mobile && (tab === 'review' || tab === 'plugins')) {
    setTab('models')
  }
}, [mobile, tab])
```

6. 在条件渲染链中加入：

```tsx
) : tab === 'plugins' ? (
  <PluginSettingsPanel />
```

放在 `tab === 'sync'` 之后、`tab === 'feedback'` 之前。

- [ ] **Step 4: 添加样式**

在 `web/src/styles.css` 末尾加入：

```css
/* 插件与连接 */
.plugin-settings {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.plugin-install-links {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
}

.plugin-install-links a {
  color: var(--blue);
  text-decoration: none;
}

.plugin-pair-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-soft);
}

.plugin-pair-code {
  font-family: var(--mono);
  font-size: 30px;
  letter-spacing: 8px;
  color: var(--blue-ink);
}

.plugin-pair-box span,
.plugin-card span {
  font-size: 12px;
  color: var(--muted);
}

.plugin-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.plugin-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg);
}

.plugin-card > div {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.plugin-card b {
  font-size: 13px;
}

.plugin-card span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plugin-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: #c0392b;
}
```

- [ ] **Step 5: 构建并手工验证**

Run:

```powershell
npm --prefix web run build
```

Expected: 构建通过。

启动桌面端或 `python -m server.main` 后，打开设置页：

1. 能看到「插件与连接」页签。
2. 点「生成配对码」出现 6 位数字和倒计时。
3. 已连接列表初始为空。

- [ ] **Step 6: 提交**

```powershell
git add web/src/lib/plugins.ts web/src/components/PluginSettingsPanel.tsx web/src/components/SettingsDialog.tsx web/src/components/icons.tsx web/src/styles.css
git commit -m "feat: add plugin pairing settings panel"
```

---

## Task 6: 扩展脚手架

**Files:**
- Create: `extensions/browser/package.json`
- Create: `extensions/browser/tsconfig.json`
- Create: `extensions/browser/wxt.config.ts`
- Create: `extensions/browser/entrypoints/popup/index.html`
- Create: `extensions/browser/entrypoints/popup/main.tsx`
- Create: `extensions/browser/.gitignore`

- [ ] **Step 1: 创建目录与 package.json**

```powershell
New-Item -ItemType Directory -Force -Path extensions\browser\entrypoints\popup, extensions\browser\lib, extensions\browser\tests, extensions\browser\public\icon | Out-Null
Copy-Item src-tauri\icons\128x128.png extensions\browser\public\icon\128.png
```

`extensions/browser/package.json`：

```json
{
  "name": "paperead-connector",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "wxt",
    "build": "wxt build",
    "build:firefox": "wxt build -b firefox",
    "compile": "tsc --noEmit",
    "test": "vitest run"
  }
}
```

- [ ] **Step 2: 安装依赖**

```powershell
npm --prefix extensions/browser install react react-dom
npm --prefix extensions/browser install -D wxt @wxt-dev/module-react typescript @types/react @types/react-dom vitest jsdom
```

- [ ] **Step 3: 配置 WXT**

`extensions/browser/wxt.config.ts`：

```ts
import { defineConfig } from 'wxt'

export default defineConfig({
  modules: ['@wxt-dev/module-react'],
  manifest: ({ browser }) => ({
    name: 'Paperead Connector',
    description: '把网页上的论文送进本地 Paperead 文献库',
    version: '0.1.0',
    icons: {
      128: 'icon/128.png',
    },
    action: {
      default_title: 'Paperead Connector',
      default_icon: 'icon/128.png',
    },
    permissions: [
      'storage',
      'activeTab',
      'scripting',
      'contextMenus',
      'notifications',
    ],
    host_permissions: ['http://127.0.0.1/*'],
    optional_host_permissions: ['https://*/*', 'http://*/*'],
    ...(browser === 'firefox'
      ? {
          browser_specific_settings: {
            gecko: { id: 'connector@paperead.app' },
          },
        }
      : {}),
  }),
})
```

`extensions/browser/tsconfig.json`：

```json
{
  "extends": "./.wxt/tsconfig.json",
  "compilerOptions": {
    "jsx": "react-jsx",
    "strict": true
  }
}
```

`extensions/browser/.gitignore`：

```gitignore
node_modules
.output
.wxt
```

- [ ] **Step 4: 创建最小弹窗**

`extensions/browser/entrypoints/popup/index.html`：

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Paperead Connector</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="./main.tsx"></script>
  </body>
</html>
```

`extensions/browser/entrypoints/popup/main.tsx`：

```tsx
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './style.css'

createRoot(document.getElementById('root')!).render(<App />)
```

`extensions/browser/entrypoints/popup/App.tsx`（临时占位，Task 10 会替换）：

```tsx
export function App() {
  return <main style={{ padding: 16, width: 320 }}>Paperead Connector</main>
}
```

`extensions/browser/entrypoints/popup/style.css`：

```css
:root {
  color-scheme: light dark;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Microsoft YaHei", sans-serif;
}

body {
  margin: 0;
  width: 340px;
  background: #ffffff;
  color: #17181a;
}

@media (prefers-color-scheme: dark) {
  body {
    background: #17181a;
    color: #f3f4f6;
  }
}
```

- [ ] **Step 5: 生成类型并构建**

```powershell
npm --prefix extensions/browser exec wxt prepare
npm --prefix extensions/browser run build
```

Expected: 生成 `extensions/browser/.output/chrome-mv3`，无 TypeScript 错误。

- [ ] **Step 6: 提交**

```powershell
git add extensions/browser/package.json extensions/browser/package-lock.json extensions/browser/tsconfig.json extensions/browser/wxt.config.ts extensions/browser/.gitignore extensions/browser/public/icon/128.png extensions/browser/entrypoints
git commit -m "chore: scaffold browser extension"
```

---

## Task 7: 元数据提取

**Files:**
- Create: `extensions/browser/lib/types.ts`
- Create: `extensions/browser/lib/metadata.ts`
- Create: `extensions/browser/lib/metadata.test.ts`
- Create: `extensions/browser/vitest.config.ts`

- [ ] **Step 1: 写失败测试**

`extensions/browser/vitest.config.ts`：

```ts
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
  },
})
```

`extensions/browser/lib/metadata.test.ts`：

```ts
import { beforeEach, describe, expect, it } from 'vitest'
import { extractMetadataInPage } from './metadata'

function render(head: string) {
  document.head.innerHTML = head
  document.body.innerHTML = ''
}

describe('extractMetadataInPage', () => {
  beforeEach(() => {
    document.head.innerHTML = ''
    document.body.innerHTML = ''
  })

  it('reads arXiv citation metadata', () => {
    render(`
      <meta name="citation_title" content="A Test Paper" />
      <meta name="citation_author" content="Ada Lovelace" />
      <meta name="citation_author" content="Alan Turing" />
      <meta name="citation_publication_date" content="2024-01-02" />
      <meta name="citation_doi" content="10.1000/test" />
      <meta name="citation_pdf_url" content="https://arxiv.org/pdf/2401.00001" />
      <meta name="citation_abstract" content="Testing ingestion." />
    `)

    const meta = extractMetadataInPage('https://arxiv.org/abs/2401.00001')
    expect(meta.title).toBe('A Test Paper')
    expect(meta.authors).toBe('Ada Lovelace, Alan Turing')
    expect(meta.year).toBe(2024)
    expect(meta.doi).toBe('10.1000/test')
    expect(meta.source).toBe('arxiv')
    expect(meta.source_id).toBe('arxiv:2401.00001')
    expect(meta.pdf_url).toBe('https://arxiv.org/pdf/2401.00001')
  })

  it('derives arXiv pdf url from abs url', () => {
    render('')

    const meta = extractMetadataInPage('https://arxiv.org/abs/2401.00001v2')
    expect(meta.source_id).toBe('arxiv:2401.00001')
    expect(meta.pdf_url).toBe('https://arxiv.org/pdf/2401.00001')
  })

  it('reads JSON-LD scholarly article', () => {
    render(`
      <script type="application/ld+json">
        {
          "@type": "ScholarlyArticle",
          "headline": "JSON Paper",
          "author": [{ "name": "Grace Hopper" }],
          "datePublished": "2023-06-01",
          "isPartOf": { "name": "Journal of Tests" },
          "description": "From JSON-LD."
        }
      </script>
    `)

    const meta = extractMetadataInPage('https://example.com/article/123')
    expect(meta.title).toBe('JSON Paper')
    expect(meta.authors).toBe('Grace Hopper')
    expect(meta.year).toBe(2023)
    expect(meta.venue).toBe('Journal of Tests')
    expect(meta.abstract).toBe('From JSON-LD.')
  })

  it('detects PubMed id', () => {
    render('')
    const meta = extractMetadataInPage('https://pubmed.ncbi.nlm.nih.gov/12345678/')
    expect(meta.source).toBe('pubmed')
    expect(meta.source_id).toBe('pubmed:12345678')
  })
})
```

- [ ] **Step 2: 运行确认失败**

```powershell
npm --prefix extensions/browser test
```

Expected: FAIL，`Cannot find module './metadata'`。

- [ ] **Step 3: 实现类型与提取函数**

`extensions/browser/lib/types.ts`：

```ts
export interface PaperMetadata {
  source: string
  source_id: string
  title: string
  authors: string
  abstract: string
  doi: string
  url: string
  year: number | null
  venue: string
  pdf_url: string
}

export interface PaperSummary {
  id: string
  title: string
  status: string
  open_url: string
  [key: string]: unknown
}

export interface TaskState {
  id: string
  tabId: number
  title: string
  status: 'working' | 'ready' | 'failed' | 'existing'
  message: string
  paperId?: string
  openUrl?: string
}
```

`extensions/browser/lib/metadata.ts`：

```ts
import type { PaperMetadata } from './types'

/**
 * 在页面上下文执行。必须保持自包含：不能引用模块级函数或 import，
 * 因为 chrome.scripting.executeScript 只会序列化函数体。
 */
export function extractMetadataInPage(href = location.href): PaperMetadata {
  const meta = (name: string) => {
    const element = document.querySelector(
      `meta[name="${name}"], meta[property="${name}"]`,
    ) as HTMLMetaElement | null
    return (element?.content || '').trim()
  }

  const metas = (name: string) =>
    Array.from(
      document.querySelectorAll(`meta[name="${name}"]`) as NodeListOf<HTMLMetaElement>,
    )
      .map((element) => (element.content || '').trim())
      .filter(Boolean)

  let ld: Record<string, unknown> | null = null
  for (const script of Array.from(
    document.querySelectorAll('script[type="application/ld+json"]'),
  )) {
    try {
      const parsed = JSON.parse(script.textContent || '')
      const items = Array.isArray(parsed) ? parsed : [parsed]
      const hit = items.find((item) =>
        String((item as Record<string, unknown>)?.['@type'] || '').match(
          /ScholarlyArticle|Article/,
        ),
      )
      if (hit) ld = hit as Record<string, unknown>
    } catch {
      // 单个 JSON-LD 损坏不影响其它元数据
    }
  }

  const html = document.documentElement.innerHTML

  const arxiv = href.match(
    /arxiv\.org\/(?:abs|pdf)\/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?/i,
  )
  const pubmed = href.match(/pubmed\.ncbi\.nlm\.nih\.gov\/(\d+)/i)
  const pmc = href.match(/pmc\.ncbi\.nlm\.nih\.gov\/articles\/(PMC\d+)/i)

  const doiMeta = meta('citation_doi')
  const doiMatch = (doiMeta || href || html).match(/10\.\d{4,9}\/[^\s"'<>]+/)
  const doi = (doiMeta || (doiMatch ? doiMatch[0] : '')).replace(/[.,;)\]]+$/, '')

  let source = 'web'
  let sourceId = ''
  if (arxiv) {
    source = 'arxiv'
    sourceId = `arxiv:${arxiv[1]}`
  } else if (pubmed) {
    source = 'pubmed'
    sourceId = `pubmed:${pubmed[1]}`
  } else if (pmc) {
    source = 'pmc'
    sourceId = `pmc:${pmc[1].toUpperCase()}`
  } else if (doi) {
    source = 'doi'
    sourceId = `doi:${doi.toLowerCase()}`
  }

  const authors =
    metas('citation_author').join(', ') ||
    (() => {
      const author = ld?.author
      if (Array.isArray(author)) {
        return author
          .map((item) =>
            typeof item === 'string'
              ? item
              : String((item as Record<string, unknown>)?.name || ''),
          )
          .filter(Boolean)
          .join(', ')
      }
      if (typeof author === 'string') return author
      if (author && typeof author === 'object') {
        return String((author as Record<string, unknown>).name || '')
      }
      return ''
    })()

  const date =
    meta('citation_publication_date') ||
    (typeof ld?.datePublished === 'string' ? ld.datePublished : '')
  const yearMatch = date.match(/(19|20)\d{2}/)
  const year = yearMatch ? Number(yearMatch[0]) : null

  const pdfLink = document.querySelector(
    'link[type="application/pdf"]',
  ) as HTMLLinkElement | null
  const anchor = Array.from(document.querySelectorAll('a[href]')).find((item) =>
    /\.pdf($|[?#])/i.test((item as HTMLAnchorElement).href || ''),
  ) as HTMLAnchorElement | null

  return {
    source,
    source_id: sourceId,
    title:
      meta('citation_title') ||
      (typeof ld?.headline === 'string' ? ld.headline : '') ||
      meta('og:title') ||
      document.title ||
      'Untitled paper',
    authors,
    abstract:
      meta('citation_abstract') ||
      (typeof ld?.description === 'string' ? ld.description : '') ||
      meta('og:description'),
    doi,
    url: href,
    year,
    venue:
      meta('citation_journal_title') ||
      meta('citation_conference_title') ||
      (() => {
        const part = ld?.isPartOf
        return part && typeof part === 'object'
          ? String((part as Record<string, unknown>).name || '')
          : ''
      })(),
    pdf_url:
      meta('citation_pdf_url') ||
      pdfLink?.href ||
      (arxiv ? `https://arxiv.org/pdf/${arxiv[1]}` : '') ||
      anchor?.href ||
      '',
  }
}
```

- [ ] **Step 4: 运行测试**

```powershell
npm --prefix extensions/browser test
```

Expected: 4 passed。

- [ ] **Step 5: 提交**

```powershell
git add extensions/browser/lib/types.ts extensions/browser/lib/metadata.ts extensions/browser/lib/metadata.test.ts extensions/browser/vitest.config.ts
git commit -m "feat: extract paper metadata in browser pages"
```

---

## Task 8: 本地 API 客户端与 PDF 获取

**Files:**
- Create: `extensions/browser/lib/api.ts`
- Create: `extensions/browser/lib/pdf.ts`

- [ ] **Step 1: 实现 API 客户端**

`extensions/browser/lib/api.ts`：

```ts
import { browser } from 'wxt/browser'
import type { PaperMetadata, PaperSummary } from './types'

const PORTS = Array.from({ length: 10 }, (_, index) => 17321 + index)
let baseCache = ''

export async function discoverBase(force = false): Promise<string> {
  if (baseCache && !force) return baseCache

  const saved = await browser.storage.local.get('base')
  if (!force && typeof saved.base === 'string' && saved.base) {
    baseCache = saved.base
    return baseCache
  }

  for (const port of PORTS) {
    const base = `http://127.0.0.1:${port}`
    try {
      const response = await fetch(`${base}/api/ext/v1/ping`, {
        signal: AbortSignal.timeout(300),
      })
      if (!response.ok) continue
      const data = (await response.json()) as { app?: string }
      if (data.app !== 'paperead') continue
      baseCache = base
      await browser.storage.local.set({ base })
      return base
    } catch {
      // 该端口没有 Paperead，继续探测下一个
    }
  }
  throw new Error('Paperead 未启动，请先打开桌面端')
}

async function token(): Promise<string> {
  const saved = await browser.storage.local.get('pluginToken')
  return typeof saved.pluginToken === 'string' ? saved.pluginToken : ''
}

export async function isPaired(): Promise<boolean> {
  return Boolean(await token())
}

export async function pair(code: string): Promise<void> {
  const base = await discoverBase()
  const response = await fetch(`${base}/api/ext/v1/pair`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, name: 'Paperead Connector' }),
  })
  const data = (await response.json().catch(() => ({}))) as { detail?: string; token?: string }
  if (!response.ok || !data.token) {
    throw new Error(data.detail || `配对失败（HTTP ${response.status}）`)
  }
  await browser.storage.local.set({ pluginToken: data.token })
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = await discoverBase()
  const headers = new Headers(init.headers)
  headers.set('Authorization', `Bearer ${await token()}`)
  const response = await fetch(`${base}${path}`, { ...init, headers })
  const data = (await response.json().catch(() => ({}))) as { detail?: string }
  if (response.status === 401) {
    await browser.storage.local.remove('pluginToken')
    throw new Error('插件未连接或授权已撤销，请重新配对')
  }
  if (!response.ok) {
    throw new Error(data.detail || `请求失败（HTTP ${response.status}）`)
  }
  return data as T
}

export async function lookup(meta: PaperMetadata): Promise<{
  found: boolean
  paper?: PaperSummary
}> {
  const params = new URLSearchParams()
  if (meta.source_id) params.set('source_id', meta.source_id)
  if (meta.doi) params.set('doi', meta.doi)
  if (meta.url) params.set('url', meta.url)
  return request(`/api/ext/v1/papers/lookup?${params.toString()}`)
}

export async function ingest(
  bytes: Uint8Array,
  meta: PaperMetadata,
  folderId = '',
): Promise<{ paper: PaperSummary; already: boolean }> {
  const form = new FormData()
  const blob = new Blob([bytes], { type: 'application/pdf' })
  form.append('file', new File([blob], 'paper.pdf', { type: 'application/pdf' }))
  form.append('metadata', JSON.stringify(meta))
  if (folderId) form.append('folder_id', folderId)
  return request('/api/ext/v1/papers/ingest', { method: 'POST', body: form })
}

export async function getPaper(paperId: string): Promise<PaperSummary> {
  const data = await request<{ paper: PaperSummary; open_url: string }>(
    `/api/ext/v1/papers/${encodeURIComponent(paperId)}`,
  )
  return { ...data.paper, open_url: data.open_url }
}

export async function listFolders(): Promise<
  Array<{ id: string; name: string; count: number }>
> {
  const data = await request<{
    folders: Array<{ id: string; name: string; count: number }>
  }>('/api/ext/v1/folders')
  return data.folders
}
```

- [ ] **Step 2: 实现 PDF 获取**

`extensions/browser/lib/pdf.ts`：

```ts
import { browser } from 'wxt/browser'
import type { PaperMetadata } from './types'

const MAX_BYTES = 100 * 1024 * 1024

function isPdf(bytes: Uint8Array): boolean {
  return (
    bytes.length >= 5 &&
    bytes[0] === 0x25 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x44 &&
    bytes[3] === 0x46 &&
    bytes[4] === 0x2d
  )
}

export async function ensureHostPermission(url: string): Promise<void> {
  const origin = `${new URL(url).origin}/*`
  const has = await browser.permissions.contains({ origins: [origin] })
  if (has) return
  const granted = await browser.permissions.request({ origins: [origin] })
  if (!granted) throw new Error('未授权访问该站点，无法下载 PDF')
}

async function fetchInBackground(url: string): Promise<Uint8Array> {
  const response = await fetch(url, { credentials: 'include', redirect: 'follow' })
  if (!response.ok) throw new Error(`PDF 下载失败（HTTP ${response.status}）`)
  const length = Number(response.headers.get('content-length') || 0)
  if (length > MAX_BYTES) throw new Error('PDF 超过 100MB，请手动导入')
  const bytes = new Uint8Array(await response.arrayBuffer())
  if (!isPdf(bytes)) {
    throw new Error('该 PDF 需要登录或验证，请下载后拖入 Paperead')
  }
  if (bytes.length > MAX_BYTES) throw new Error('PDF 超过 100MB，请手动导入')
  return bytes
}

async function fetchInTab(tabId: number, url: string): Promise<Uint8Array> {
  const [injected] = await browser.scripting.executeScript({
    target: { tabId },
    func: async (target: string) => {
      const response = await fetch(target, { credentials: 'include' })
      if (!response.ok) return { error: `HTTP ${response.status}` }
      const bytes = new Uint8Array(await response.arrayBuffer())
      if (bytes.length > 30 * 1024 * 1024) return { error: 'too-large' }
      if (
        bytes.length < 5 ||
        bytes[0] !== 0x25 ||
        bytes[1] !== 0x50 ||
        bytes[2] !== 0x44
      ) {
        return { error: 'not-pdf' }
      }
      let binary = ''
      for (let index = 0; index < bytes.length; index += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000))
      }
      return { base64: btoa(binary) }
    },
    args: [url],
  })
  const result = injected.result as { base64?: string; error?: string }
  if (!result?.base64) {
    if (result?.error === 'too-large') {
      throw new Error('PDF 较大，请下载后拖入 Paperead')
    }
    throw new Error(
      result?.error === 'not-pdf'
        ? '该 PDF 需要登录或验证，请下载后拖入 Paperead'
        : 'PDF 获取失败',
    )
  }
  const binary = atob(result.base64)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return bytes
}

export async function acquirePdf(
  meta: PaperMetadata,
  tabId?: number,
): Promise<Uint8Array> {
  if (!meta.pdf_url) throw new Error('没有找到 PDF 地址')
  await ensureHostPermission(meta.pdf_url)
  try {
    return await fetchInBackground(meta.pdf_url)
  } catch (error) {
    if (!tabId) throw error
    return fetchInTab(tabId, meta.pdf_url)
  }
}
```

- [ ] **Step 3: 类型检查**

```powershell
npm --prefix extensions/browser run compile
```

Expected: 通过。

- [ ] **Step 4: 提交**

```powershell
git add extensions/browser/lib/api.ts extensions/browser/lib/pdf.ts
git commit -m "feat: add extension API client and PDF acquisition"
```

---

## Task 9: 后台保存任务

**Files:**
- Create: `extensions/browser/entrypoints/background.ts`

- [ ] **Step 1: 实现后台任务**

`extensions/browser/entrypoints/background.ts`：

```ts
import { browser } from 'wxt/browser'
import { defineBackground } from 'wxt/sandbox'
import { acquirePdf } from '../lib/pdf'
import { getPaper, ingest, isPaired, lookup } from '../lib/api'
import { extractMetadataInPage } from '../lib/metadata'
import type { PaperMetadata, TaskState } from '../lib/types'

const MENU_SAVE_PAGE = 'save-page'
const MENU_SAVE_LINK = 'save-link'

function isPdfUrl(url: string): boolean {
  return /\.pdf($|[?#])/i.test(url)
}

async function setTask(task: TaskState): Promise<void> {
  const stored = await browser.storage.local.get('tasks')
  const tasks = (stored.tasks || {}) as Record<string, TaskState>
  tasks[task.id] = task
  await browser.storage.local.set({ tasks })
}

async function notify(title: string, message: string): Promise<void> {
  try {
    await browser.notifications.create({
      type: 'basic',
      iconUrl: browser.runtime.getURL('icon/128.png'),
      title,
      message,
    })
  } catch {
    // 通知被系统关闭时不影响保存任务
  }
}

async function readMeta(tabId: number): Promise<PaperMetadata> {
  const tab = await browser.tabs.get(tabId)
  const url = tab.url || ''
  if (isPdfUrl(url)) {
    return {
      source: 'pdf',
      source_id: '',
      title: decodeURIComponent(url.split('/').pop() || 'paper.pdf'),
      authors: '',
      abstract: '',
      doi: '',
      url,
      year: null,
      venue: '',
      pdf_url: url,
    }
  }
  const [injected] = await browser.scripting.executeScript({
    target: { tabId },
    func: extractMetadataInPage,
  })
  return injected.result as PaperMetadata
}

async function pollUntilDone(paperId: string, task: TaskState): Promise<void> {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const paper = await getPaper(paperId)
    if (paper.status === 'ready') {
      await setTask({
        ...task,
        status: 'ready',
        message: '已存入文献库',
        paperId,
        openUrl: (paper.open_url as string) || '',
      })
      await notify('已存入 Paperead', task.title)
      return
    }
    if (paper.status === 'failed') {
      await setTask({
        ...task,
        status: 'failed',
        message: '解析失败，可在文献库中重试',
        paperId,
      })
      await notify('解析失败', task.title)
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 1500))
  }
  await setTask({
    ...task,
    status: 'failed',
    message: '解析时间较长，请到文献库查看',
    paperId,
  })
}

async function saveTab(
  tabId: number,
  folderId = '',
  pdfUrlOverride = '',
): Promise<TaskState> {
  if (!(await isPaired())) {
    throw new Error('请先在扩展弹窗中完成配对')
  }
  const meta = await readMeta(tabId)
  if (pdfUrlOverride) meta.pdf_url = pdfUrlOverride

  const task: TaskState = {
    id: `${tabId}-${Date.now()}`,
    tabId,
    title: meta.title,
    status: 'working',
    message: '正在检查文献库',
  }
  await setTask(task)

  const existing = await lookup(meta)
  if (existing.found && existing.paper) {
    await setTask({
      ...task,
      status: 'existing',
      message: '已在文献库中',
      paperId: existing.paper.id,
      openUrl: existing.paper.open_url,
    })
    return task
  }

  const bytes = await acquirePdf(meta, tabId)
  await setTask({ ...task, message: '正在上传' })
  const saved = await ingest(bytes, meta, folderId)
  await setTask({
    ...task,
    status: 'working',
    message: '正在解析',
    paperId: saved.paper.id,
    openUrl: saved.paper.open_url,
  })
  await pollUntilDone(saved.paper.id, task)
  return task
}

export default defineBackground(() => {
  browser.runtime.onInstalled.addListener(() => {
    void browser.contextMenus.removeAll().then(() => {
      browser.contextMenus.create({
        id: MENU_SAVE_PAGE,
        title: '保存到 Paperead',
        contexts: ['page', 'selection'],
      })
      browser.contextMenus.create({
        id: MENU_SAVE_LINK,
        title: '保存链接指向的 PDF',
        contexts: ['link'],
      })
    })
  })

  browser.contextMenus.onClicked.addListener((info, tab) => {
    if (!tab?.id) return
    if (info.menuItemId === MENU_SAVE_PAGE) {
      void saveTab(tab.id).catch((error) =>
        notify('保存失败', error instanceof Error ? error.message : '未知错误'),
      )
    }
    if (info.menuItemId === MENU_SAVE_LINK && info.linkUrl) {
      void saveTab(tab.id, '', info.linkUrl).catch((error) =>
        notify('保存失败', error instanceof Error ? error.message : '未知错误'),
      )
    }
  })

  browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    const payload = message as {
      type: string
      tabId?: number
      folderId?: string
      pdfUrl?: string
    }
    if (payload.type === 'preview' && payload.tabId) {
      readMeta(payload.tabId)
        .then((meta) => sendResponse({ meta }))
        .catch((error) => sendResponse({ error: String(error.message || error) }))
      return true
    }
    if (payload.type === 'save' && payload.tabId) {
      saveTab(payload.tabId, payload.folderId, payload.pdfUrl)
        .then((task) => sendResponse({ task }))
        .catch((error) => sendResponse({ error: String(error.message || error) }))
      return true
    }
    return false
  })
})
```

- [ ] **Step 2: 类型检查**

```powershell
npm --prefix extensions/browser run compile
```

Expected: 通过。若 `defineBackground` 未定义，先运行一次 `npx --prefix extensions/browser wxt prepare` 生成 `.wxt` 类型。

- [ ] **Step 3: 提交**

```powershell
git add extensions/browser/entrypoints/background.ts
git commit -m "feat: add background save pipeline"
```

---

## Task 10: 弹窗 UI

**Files:**
- Modify: `extensions/browser/entrypoints/popup/App.tsx`
- Modify: `extensions/browser/entrypoints/popup/style.css`

- [ ] **Step 1: 实现弹窗**

`extensions/browser/entrypoints/popup/App.tsx`：

```tsx
import { useCallback, useEffect, useState } from 'react'
import { browser } from 'wxt/browser'
import { isPaired, listFolders, pair } from '../../lib/api'
import { ensureHostPermission } from '../../lib/pdf'
import type { PaperMetadata, TaskState } from '../../lib/types'

interface Folder {
  id: string
  name: string
  count: number
}

export function App() {
  const [paired, setPaired] = useState(false)
  const [code, setCode] = useState('')
  const [meta, setMeta] = useState<PaperMetadata | null>(null)
  const [folders, setFolders] = useState<Folder[]>([])
  const [folderId, setFolderId] = useState('')
  const [task, setTask] = useState<TaskState | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setError('')
    const pairedNow = await isPaired()
    setPaired(pairedNow)
    if (!pairedNow) return

    try {
      setFolders(await listFolders())
    } catch {
      // 文件夹加载失败不影响保存
    }

    const [tab] = await browser.tabs.query({ active: true, currentWindow: true })
    if (!tab?.id) return
    const response = (await browser.runtime.sendMessage({
      type: 'preview',
      tabId: tab.id,
    })) as { meta?: PaperMetadata; error?: string }
    if (response.error) {
      setError(response.error)
      return
    }
    if (response.meta) setMeta(response.meta)
  }, [])

  useEffect(() => {
    void load()
    const listener = (
      changes: Record<string, { newValue?: unknown }>,
      area: string,
    ) => {
      if (area !== 'local' || !changes.tasks) return
      const tasks = (changes.tasks.newValue || {}) as Record<string, TaskState>
      const latest = Object.values(tasks).sort((a, b) =>
        a.id < b.id ? 1 : -1,
      )[0]
      setTask(latest || null)
    }
    browser.storage.onChanged.addListener(listener)
    return () => browser.storage.onChanged.removeListener(listener)
  }, [load])

  const connect = async () => {
    setBusy(true)
    try {
      await pair(code)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '配对失败')
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true })
    if (!tab?.id) return
    setBusy(true)
    setError('')
    try {
      // permissions.request 必须在用户手势里调用，放在弹窗比后台可靠
      if (meta?.pdf_url) await ensureHostPermission(meta.pdf_url)
      const response = (await browser.runtime.sendMessage({
        type: 'save',
        tabId: tab.id,
        folderId,
        pdfUrl: meta?.pdf_url && meta.pdf_url !== tab.url ? meta.pdf_url : '',
      })) as { task?: TaskState; error?: string }
      if (response.error) {
        setError(response.error)
        return
      }
      if (response.task) setTask(response.task)
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  if (!paired) {
    return (
      <main className="popup">
        <header>
          <h1>Paperead Connector</h1>
          <p>先在 Paperead 设置 →「插件与连接」生成配对码。</p>
        </header>
        <input
          value={code}
          onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))}
          placeholder="6 位配对码"
          inputMode="numeric"
        />
        <button onClick={() => void connect()} disabled={busy || code.length !== 6}>
          {busy ? '连接中…' : '连接'}
        </button>
        {error && <p className="error">{error}</p>}
      </main>
    )
  }

  return (
    <main className="popup">
      <header>
        <h1>Paperead Connector</h1>
        <p>{meta?.title || '正在读取当前页面…'}</p>
        {meta?.authors && <p className="authors">{meta.authors}</p>}
      </header>

      {meta && (
        <dl className="meta">
          <div>
            <dt>年份</dt>
            <dd>{meta.year || '—'}</dd>
          </div>
          <div>
            <dt>期刊</dt>
            <dd>{meta.venue || '—'}</dd>
          </div>
          <div>
            <dt>DOI</dt>
            <dd>{meta.doi || '—'}</dd>
          </div>
        </dl>
      )}

      <label className="folder">
        保存到
        <select value={folderId} onChange={(event) => setFolderId(event.target.value)}>
          <option value="">全部文献</option>
          {folders.map((folder) => (
            <option key={folder.id} value={folder.id}>
              {folder.name}（{folder.count}）
            </option>
          ))}
        </select>
      </label>

      <button onClick={() => void save()} disabled={busy || !meta}>
        {busy ? '保存中…' : '保存到 Paperead'}
      </button>

      {task && (
        <p className={`task ${task.status}`}>
          {task.message}
          {task.openUrl && (
            <button className="link" onClick={() => void browser.tabs.create({ url: task.openUrl! })}>
              打开阅读页
            </button>
          )}
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </main>
  )
}
```

- [ ] **Step 2: 样式**

`extensions/browser/entrypoints/popup/style.css`：

```css
:root {
  color-scheme: light dark;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Microsoft YaHei", sans-serif;
}

body {
  margin: 0;
  width: 340px;
  background: #ffffff;
  color: #17181a;
}

.popup {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
}

h1 {
  margin: 0;
  font-size: 15px;
}

p {
  margin: 4px 0 0;
  font-size: 12px;
  color: #6b7280;
  line-height: 1.5;
}

.authors {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

input,
select,
button {
  width: 100%;
  box-sizing: border-box;
  font: inherit;
  font-size: 13px;
}

input,
select {
  height: 32px;
  padding: 0 8px;
  border: 1px solid #dde5ef;
  border-radius: 7px;
  background: #ffffff;
  color: inherit;
}

button {
  height: 34px;
  border: 0;
  border-radius: 7px;
  background: #14508c;
  color: #ffffff;
  cursor: pointer;
}

button:disabled {
  opacity: 0.55;
  cursor: default;
}

.meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 12px;
  margin: 0;
}

.meta div {
  min-width: 0;
}

.meta dt {
  font-size: 11px;
  color: #6b7280;
}

.meta dd {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

.folder {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #6b7280;
}

.folder select {
  flex: 1;
}

.task {
  padding: 8px 10px;
  border-radius: 7px;
  background: #f4f8fd;
  color: #2b3a4a;
}

.task.failed {
  background: #fff1f0;
  color: #a61b1b;
}

.task.ready,
.task.existing {
  background: #eef8f1;
  color: #1a6b38;
}

.task .link {
  width: auto;
  height: auto;
  margin-left: 8px;
  padding: 0;
  background: none;
  color: #14508c;
  text-decoration: underline;
}

.error {
  color: #b42318;
}

@media (prefers-color-scheme: dark) {
  body {
    background: #17181a;
    color: #f3f4f6;
  }

  p,
  .meta dt,
  .folder {
    color: #9aa1ac;
  }

  input,
  select {
    border-color: #3a3d42;
    background: #202124;
  }

  .task {
    background: #202a35;
    color: #d7e2ee;
  }

  .task.failed {
    background: #3a2020;
    color: #ffb4ab;
  }

  .task.ready,
  .task.existing {
    background: #1d3325;
    color: #9be3b7;
  }
}
```

- [ ] **Step 3: 构建**

```powershell
npm --prefix extensions/browser run compile
npm --prefix extensions/browser run build
```

Expected: 生成 `extensions/browser/.output/chrome-mv3`。

- [ ] **Step 4: 提交**

```powershell
git add extensions/browser/entrypoints/popup
git commit -m "feat: add extension popup UI"
```

---

## Task 11: 端到端验收

**Files:** 无新增文件

- [ ] **Step 1: 启动服务并生成配对码**

终端 A：

```powershell
.venv-dev\Scripts\python.exe -m server.main
```

浏览器打开 `http://127.0.0.1:8010`，进入设置 →「插件与连接」→ 生成配对码。

如果使用桌面端，端口以桌面端实际端口为准（优先 17321）。

- [ ] **Step 2: 加载扩展**

Chrome/Edge 打开 `chrome://extensions`，开启开发者模式，选择「加载已解压的扩展程序」，目录选：

```text
extensions/browser/.output/chrome-mv3
```

- [ ] **Step 3: 验收 arXiv**

1. 打开 `https://arxiv.org/abs/2401.00001`。
2. 点扩展图标，输入配对码并连接。
3. 弹窗应显示标题、作者、年份、DOI。
4. 点「保存到 Paperead」。
5. 等待状态变为「已存入文献库」。
6. 点「打开阅读页」，确认打开 `http://127.0.0.1:<port>/?p=<paper_id>`，标题和页数正确。

- [ ] **Step 4: 验收重复保存**

再次保存同一篇论文：

- 不产生第二条记录。
- 弹窗或通知显示「已在文献库中」。

- [ ] **Step 5: 验收应用未启动**

关闭 Paperead 服务后点保存：

- 弹窗提示「Paperead 未启动，请先打开桌面端」。
- 不出现未捕获异常。

- [ ] **Step 6: 验收撤销**

设置页撤销插件 token，再点保存：

- 弹窗提示「插件未连接或授权已撤销，请重新配对」。
- 重新配对后可以继续保存。

- [ ] **Step 7: 运行完整回归**

```powershell
.venv-dev\Scripts\python.exe -m pytest tests/test_plugin_auth.py tests/test_ext_api.py tests/test_server_security.py -v
npm --prefix web run build
npm --prefix extensions/browser test
npm --prefix extensions/browser run build
```

Expected: 全部通过。

- [ ] **Step 8: 提交验收修复（如有）**

```powershell
git add server/plugin_auth.py server/ext_api.py server/main.py tests/test_plugin_auth.py tests/test_ext_api.py web/src/lib/plugins.ts web/src/components/PluginSettingsPanel.tsx web/src/components/SettingsDialog.tsx web/src/components/icons.tsx web/src/styles.css extensions/browser
git commit -m "fix: address plugin end-to-end verification issues"
```

---

## 风险与回退

| 风险 | 处理 |
|---|---|
| 出版商 PDF 带登录墙，后台 fetch 拿到 HTML | 已按魔数拒绝并提示下载后拖入；不在第一版绕过付费墙 |
| 扩展 optional host permission 被拒绝 | 明确提示「未授权访问该站点」，不静默失败 |
| MV3 service worker 被回收 | 任务状态写入 `storage.local`；重新打开弹窗可从任务列表恢复；P2 再引入 offscreen document |
| 端口非 17321 | 扩展探测 17321–17330；仍失败时提示手动启动 |
| 已有未提交改动被误提交 | 所有 `git add` 均按文件名添加，禁止 `git add -A` |

## 完成标准

- P0：curl 可以完成配对、入库、查重、状态轮询、撤销；主 `/api` 安全测试不回归。
- P1：Chrome/Edge 在 arXiv/PubMed 一键入库；重复保存无副本；应用未启动和 token 撤销有明确提示。
- 设计文档与实现保持一致；如实现中调整接口，先更新 `docs/superpowers/specs/2026-11-17-browser-extension-plugin-design.md`。
