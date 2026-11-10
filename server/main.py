"""Paperead 本地服务。

为什么需要它：国内厂商的 API 基本都不允许浏览器直连（CORS），
所以「前端填 Key、直接调模型」这条路走不通，必须有一个本地代理。
另外密钥只在浏览器 localStorage 里，随请求以 body 传给本服务，服务端不落盘。

启动：
    pip install -e ".[server]"
    python -m server.main          # 或 uvicorn server.main:app --port 8000

离线联调（不消耗额度、不需要 Key）：
    PAPEREAD_MOCK=1 python -m server.main
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
import pymupdf
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from paperead import __version__
from paperead.env import env
from paperead.formula import recognize_equations, recognize_latex
from paperead.highlights import CATEGORIES as HIGHLIGHT_CATEGORIES
from paperead.highlights import extract_highlights
from paperead.layout import Block, parse_pdf
from paperead.llm import LLMConfig, OpenAICompatBackend
from paperead.tutor import build_guide
from paperead.tutor.guide import GUIDE_VERSION
from paperead.tutor import memory as memory_engine
from paperead.tutor import qa as qa_engine
from paperead.tutor import web as web_search
from paperead.tutor.citations import find as find_citations, resolve as resolve_citations
from paperead.tutor.lineage import LINEAGE_VERSION, build_lineage
from paperead.tutor.provenance import enrich as enrich_provenance
from paperead.tutor.references import Reference, parse as parse_references
from paperead.translate import (
    build_glossary,
    quality_ok,
    translate_block,
    translate_paper,
)
from server.llm_proxy import build_llm_proxy_router
from server.onboarding import OnboardingStore, build_onboarding_router
from server.store import (
    ATTACHMENT_LIMIT,
    FolderExistsError,
    FolderNameError,
    LibraryStore,
)
from server.scholar_api import build_router as build_scholar_router
from server.scholaread import build_router as build_scholaread_router
from server import zotero_web
from server.research import ResearchStore
from server.research import build_router as build_research_router
from server.research.abstracts import fill_one as fill_abstract
from server.research.search import LibrarySearch
from server.research.search_api import build_router as build_internal_router
from server.research.tools_api import build_router as build_tools_router
from server.toolchain_api import router as toolchain_router
from server.runtime import (
    allowed_origins,
    bind_host,
    default_data_dir,
    ensure_data_dir,
    is_frozen,
    web_dist,
)

log = logging.getLogger("paperead.server")

WEB_DIST = web_dist()
DATA_DIR = ensure_data_dir(default_data_dir())
PORT = int(env("PAPEREAD_PORT", "8010"))
TOKEN = env("PAPEREAD_TOKEN", "")
ORIGINS = allowed_origins(PORT)
MOCK = env("PAPEREAD_MOCK") == "1"
# 公式识别的整体时间预算：超时就先让论文可读，剩余公式保留裁图
FORMULA_TIMEOUT = float(env("PAPEREAD_FORMULA_TIMEOUT", "180"))

# 版本更新：Gitee（国内主通道，固定 tag `latest` 的发行版）优先，GitHub 与镜像作为回退。
UPDATE_ENDPOINTS = (
    "https://gitee.com/deng_xiaohui/paperead/releases/download/latest/latest.json",
    "https://github.com/xiaohui-plus/Paperead/releases/latest/download/latest.json",
    "https://ghproxy.net/https://github.com/xiaohui-plus/Paperead/releases/latest/download/latest.json",
)
UPDATE_RELEASE_PAGE = "https://gitee.com/deng_xiaohui/paperead/releases"
UPDATE_TTL = 24 * 3600
_update_cache: dict = {"checked_at": 0.0, "payload": None}

store = LibraryStore(DATA_DIR)
research_store = ResearchStore(DATA_DIR / "research")
onboarding_store = OnboardingStore(DATA_DIR)


def _has_user_data() -> bool:
    """升级用户已经有文献或研究项目，不再触发新手教程。"""
    return bool(store.list()) or bool(
        research_store.list_projects(include_archived=True)
    )


app = FastAPI(title="Paperead", version=__version__)

# 安装包不带 pandoc：桌面版首次启动在后台下载一次，之后离线可用。
# 设 PAPEREAD_PANDOC_AUTODOWNLOAD=0 可关闭，测试和开发环境默认不下载。
if is_frozen() and env("PAPEREAD_PANDOC_AUTODOWNLOAD", "1") != "0":
    from server.research import pandoc_setup

    pandoc_setup.start_if_missing()

# 项目/文件/会话等具体路由必须先于下面的 /api/research/{path:path} 代理注册，
# 否则会被通配代理拦截，误报“研究 agent 尚未启动”。
def _schedule_parse(paper_id: str) -> None:
    """加入文献库后立刻后台解析（_parse_task 在模块末尾定义，调用时已就绪）。"""
    asyncio.create_task(_parse_task(paper_id))


app.include_router(build_llm_proxy_router(DATA_DIR, TOKEN))
app.include_router(
    build_research_router(research_store, store, _schedule_parse, DATA_DIR)
)
app.include_router(toolchain_router)
app.include_router(build_onboarding_router(onboarding_store, _has_user_data))
app.include_router(build_scholaread_router(store, _schedule_parse))

# 研究 agent 的工具接口：本机调用，自带 token，不走 /api 的 cookie 鉴权。
app.include_router(
    build_internal_router(
        LibrarySearch(store, DATA_DIR / "research" / "search.db"), research_store
    )
)

@app.middleware("http")
async def guard(request: Request, call_next):
    """本地 API 防护：cookie/header token + Origin 校验。

    桌面壳把随机 token 作为 HttpOnly cookie 在首页下发，前端无需改代码；
    壳自己用 X-Paperead-Token 调 health。直接 python -m server.main 时
    TOKEN 为空，鉴权关闭，保持原有开发体验。
    """
    path = request.url.path
    if path.startswith("/api/llm-proxy/"):
        # 研究 agent 用 Authorization: Bearer <token> 调本地 LLM 代理
        if TOKEN and request.headers.get("authorization") != f"Bearer {TOKEN}":
            return JSONResponse({"detail": "unauthorized"}, status_code=403)
    elif path.startswith("/api"):
        # 旧版壳/脚本可能还在发 x-paper-reflow-token，继续接受。
        supplied = (
            request.cookies.get("pr_token")
            or request.headers.get("x-paperead-token")
            or request.headers.get("x-paper-reflow-token")
            # 图片/PDF 等媒体请求由 <img src> 发起，带不了自定义头，用查询参数传 token
            or request.query_params.get("pr_token")
        )
        if TOKEN and supplied != TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in ORIGINS:
            # 用局域网 IP / 域名访问时，页面自身的 Origin 与 Host 同源，直接放行
            parsed = urlparse(origin)
            if parsed.netloc != request.headers.get("host", ""):
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)

    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        # 前端更新后 index.html 引用的是新 hash；禁止缓存 HTML，
        # 避免用户刷新后仍加载旧的 assets/*.js。
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        # 只有本机窗口才下发口令 cookie。对局域网开放后，同网段的其它设备
        # 也能打开首页，但不能自动拿到 token（手机是靠扫二维码配对的）。
        client_host = request.client.host if request.client else ""
        local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
        if TOKEN and local and "pr_token" not in request.cookies:
            response.set_cookie(
                "pr_token", TOKEN, httponly=True, samesite="strict", path="/"
            )
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)

MOCK_LATEX = r"S_{\mathrm{AA}}[u,v]=\sum_{z\in N(u)\cap N(v)}\frac{1}{\log(d_z)}"


class LLMConfigIn(BaseModel):
    base_url: str = Field(..., description="OpenAI 兼容地址")
    api_key: str = Field(..., min_length=8)
    model: str = Field(..., min_length=1)
    timeout: float = 120.0


class FormulaReq(BaseModel):
    config: LLMConfigIn
    image_base64: str
    mime: str = "image/png"


class PingReq(BaseModel):
    config: LLMConfigIn


class ImportReq(BaseModel):
    path: str = Field(..., min_length=1, description="本机 PDF 绝对路径")


class FolderIn(BaseModel):
    name: str = ""


class ZoteroWebIn(BaseModel):
    api_key: str = ""
    user_id: str = ""
    library_type: str = "user"
    group_id: str = ""


class PaperFolderIn(BaseModel):
    folder_id: str | None = None


def _decode(image_base64: str) -> bytes:
    if "," in image_base64[:80]:  # 容忍 data:image/png;base64, 前缀
        image_base64 = image_base64.split(",", 1)[1]
    try:
        return base64.b64decode(image_base64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"图片解码失败：{e}") from e


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "mock": MOCK,
        "web": WEB_DIST.exists(),
        "version": __version__,
        "frozen": is_frozen(),
        "data": str(DATA_DIR),
    }


def _lan_ipv4() -> list[str]:
    """本机的局域网 IPv4（去掉回环），用于生成手机可连的地址。"""
    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不会真的发包，只让系统选出默认出口网卡
        probe.connect(("8.8.8.8", 80))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith("127.")]


@app.get("/api/sync/info")
async def sync_info(request: Request) -> dict:
    """设备同步：给电脑端生成二维码的内容。

    token 只回给本机请求，局域网里的其它设备拿不到；手机扫码后用它配置连接。
    电脑端默认只监听 127.0.0.1，此时 `exposed` 为 false，界面会提示如何对外开放。
    """
    client = request.client.host if request.client else ""
    local = client in {"127.0.0.1", "::1", "localhost", "testclient"}
    host = bind_host()
    exposed = host not in {"127.0.0.1", "localhost", "::1"}
    ips = _lan_ipv4() if local else []
    base = f"http://{ips[0]}:{PORT}" if exposed and ips else f"http://127.0.0.1:{PORT}"
    return {
        "ok": True,
        "base": base,
        "token": TOKEN if local else "",
        "port": PORT,
        "host": host,
        "exposed": bool(exposed and ips),
        "lan_ips": ips,
        "name": socket.gethostname(),
        "version": __version__,
    }


def _version_key(value: str) -> tuple[int, ...]:
    """0.1.10 > 0.1.9；忽略 v 前缀和 beta 之类的后缀。"""
    nums = re.findall(r"\d+", str(value or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def _update_payload(raw: dict, source: str) -> dict:
    version = str(raw.get("version") or "").strip()
    platforms = raw.get("platforms")
    if not isinstance(platforms, dict):
        platforms = {}
    win = platforms.get("windows-x86_64")
    if not isinstance(win, dict):
        win = next(
            (v for k, v in platforms.items() if "windows" in k and isinstance(v, dict)),
            {},
        )
    return {
        "current": __version__,
        "latest": version,
        "has_update": bool(version) and _version_key(version) > _version_key(__version__),
        "notes": str(raw.get("notes") or "")[:2000],
        "pub_date": str(raw.get("pub_date") or ""),
        "url": str(win.get("url") or ""),
        "release_url": UPDATE_RELEASE_PAGE,
        "source": source,
    }


async def _fetch_update() -> dict:
    endpoints: list[str] = []
    override = env("PAPEREAD_UPDATE_ENDPOINT", "")
    if override:
        endpoints.append(override)
    endpoints.extend(UPDATE_ENDPOINTS)
    last_error = ""
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        for url in endpoints:
            try:
                res = await client.get(
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": f"Paperead/{__version__}",
                    },
                )
            except Exception as e:  # 网络不通/超时：换下一个源
                last_error = str(e)[:160]
                continue
            if res.status_code != 200:
                last_error = f"HTTP {res.status_code}"
                continue
            try:
                raw = res.json()
            except Exception:
                last_error = "更新清单不是 JSON"
                continue
            if not isinstance(raw, dict) or not raw.get("version"):
                last_error = "更新清单格式不正确"
                continue
            return _update_payload(raw, url)
    raise RuntimeError(last_error or "无法连接更新服务")


@app.get("/api/update")
async def check_update(force: bool = False) -> dict:
    """检查新版本；同一天内默认复用结果，force=true 时重新拉取。"""
    now = time.time()
    cached = _update_cache.get("payload")
    if (
        isinstance(cached, dict)
        and not force
        and now - float(_update_cache.get("checked_at") or 0) < UPDATE_TTL
    ):
        return {**cached, "cached": True, "checked_at": _update_cache["checked_at"]}
    try:
        payload = await _fetch_update()
    except Exception as e:
        return {
            "current": __version__,
            "latest": "",
            "has_update": False,
            "notes": "",
            "pub_date": "",
            "url": "",
            "release_url": UPDATE_RELEASE_PAGE,
            "error": str(e)[:200],
            "cached": False,
            "checked_at": 0,
        }
    _update_cache["payload"] = payload
    _update_cache["checked_at"] = now
    return {**payload, "cached": False, "checked_at": now}


def _agent_target() -> tuple[str, str]:
    """Resolve the local research agent from the environment.

    The desktop shell reserves the agent port/token before spawning this
    process, so there is no on-disk handshake file and no token exposure to
    the frontend.
    """
    port = os.environ.get("PAPEREAD_AGENT_PORT")
    if not port:
        raise HTTPException(status_code=503, detail="研究 agent 尚未启动")
    try:
        port_number = int(port)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=f"PAPEREAD_AGENT_PORT 无效：{port}") from e
    return f"http://127.0.0.1:{port_number}", os.environ.get("PAPEREAD_AGENT_TOKEN", "")


@app.api_route(
    "/api/research/{path:path}",
    methods=["GET", "POST", "DELETE", "PATCH"],
)
async def research_proxy(path: str, request: Request):
    """Thin SSE/JSON proxy from the product API to the local Node agent.

    The frontend never sees the agent port or token; both come from the
    environment injected by the desktop shell.
    """
    base, token = _agent_target()
    url = f"{base}/v1/{path}"
    headers = {"x-agent-token": token}
    query = list(request.query_params.multi_items())

    if request.method == "GET" and path.endswith("/events"):
        client = httpx.AsyncClient(timeout=None)
        try:
            upstream = await client.send(
                client.build_request("GET", url, headers=headers, params=query),
                stream=True,
            )
        except Exception as e:
            await client.aclose()
            raise HTTPException(status_code=503, detail=f"研究 agent 不可用：{e}") from e

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache, no-transform",
                "x-accel-buffering": "no",
                "connection": "keep-alive",
            },
        )

    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            upstream = await client.request(
                request.method,
                url,
                headers={**headers, "content-type": request.headers.get("content-type", "application/json")},
                content=body,
                params=query,
            )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"研究 agent 不可用：{e}") from e

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@app.post("/api/formula")
async def formula(req: FormulaReq) -> dict:
    """一张公式裁图 → LaTeX。"""
    if MOCK:
        time.sleep(0.4)  # 模拟网络耗时，方便前端调 loading 态
        return {"ok": True, "latex": MOCK_LATEX, "ms": 400, "mock": True}

    image = _decode(req.image_base64)
    if len(image) < 200:
        raise HTTPException(status_code=400, detail="图片过小，可能不是有效的公式裁图")

    backend = OpenAICompatBackend(
        LLMConfig(
            api_key=req.config.api_key,
            base_url=req.config.base_url.rstrip("/"),
            model=req.config.model,
            timeout=req.config.timeout,
        )
    )
    started = time.time()
    try:
        latex = await recognize_latex(image, model=req.config.model, backend=backend)
    except Exception as e:
        log.warning("公式识别失败：%s", e)
        raise HTTPException(status_code=502, detail=f"模型调用失败：{e}") from e

    ms = int((time.time() - started) * 1000)
    return {"ok": bool(latex), "latex": latex, "ms": ms, "mock": False}


# 解析阶段要把公式裁图转成 LaTeX，需要一个视觉模型配置。密钥仍然只存在浏览器
# localStorage 里，前端启动/保存设置时推给本服务；只在内存里放一份，不落盘。
_vision_config: LLMConfig | None = None


class VisionConfigReq(BaseModel):
    config: LLMConfigIn | None = None


@app.post("/api/config/vision")
async def set_vision_config(req: VisionConfigReq) -> dict:
    global _vision_config
    if not req.config:
        _vision_config = None
        return {"ok": True, "configured": False}
    _vision_config = LLMConfig(
        api_key=req.config.api_key,
        base_url=req.config.base_url.rstrip("/"),
        model=req.config.model,
        timeout=req.config.timeout,
    )
    return {"ok": True, "configured": True, "model": _vision_config.model}


@app.get("/api/config/vision")
async def get_vision_config() -> dict:
    cfg = _vision_config
    return {
        "configured": cfg is not None,
        "model": cfg.model if cfg else "",
        "base_url": cfg.base_url if cfg else "",
    }


# 文献检索的运行时配置：CORE 需要 API Key，Semantic Scholar 可选。localStorage
# 是唯一来源，前端启动/保存设置时推给本服务；这里只改进程环境变量，不落盘
# （重启后前端会再同步）。
_CORE_KEY_ENV = os.environ.get("PAPEREAD_CORE_API_KEY")
_S2_KEY_ENV = os.environ.get("PAPEREAD_S2_API_KEY")


class ScholarConfigReq(BaseModel):
    coreApiKey: str | None = None
    s2ApiKey: str | None = None


def _apply_scholar_key(name: str, value: str | None, fallback: str | None) -> None:
    """推送的 Key 写进环境变量；清空时退回启动时的环境变量。"""
    if value is None:
        return
    key = value.strip()
    if key:
        os.environ[name] = key
    elif fallback:
        os.environ[name] = fallback
    else:
        os.environ.pop(name, None)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return f"{key[:4]}••••{key[-4:]}" if len(key) > 8 else "••••"


@app.post("/api/config/scholar")
async def set_scholar_config(req: ScholarConfigReq) -> dict:
    _apply_scholar_key("PAPEREAD_CORE_API_KEY", req.coreApiKey, _CORE_KEY_ENV)
    _apply_scholar_key("PAPEREAD_S2_API_KEY", req.s2ApiKey, _S2_KEY_ENV)
    return {"ok": True, "configured": bool(env("PAPEREAD_CORE_API_KEY", ""))}


@app.get("/api/config/scholar")
async def get_scholar_config() -> dict:
    core = env("PAPEREAD_CORE_API_KEY", "") or ""
    s2 = env("PAPEREAD_S2_API_KEY", "") or ""
    return {
        "core": {"configured": bool(core), "masked": _mask_key(core)},
        "s2": {"configured": bool(s2), "masked": _mask_key(s2)},
    }


@app.post("/api/ping")
async def ping(req: PingReq) -> dict:
    """连通性测试：一次最小文本调用，验证地址/密钥/模型名是否匹配。"""
    if MOCK:
        return {"ok": True, "ms": 120, "reply": "OK", "mock": True}

    backend = OpenAICompatBackend(
        LLMConfig(
            api_key=req.config.api_key,
            base_url=req.config.base_url.rstrip("/"),
            model=req.config.model,
            timeout=min(req.config.timeout, 30),
        )
    )
    started = time.time()
    try:
        reply = await backend.chat(
            [{"role": "user", "content": "只回复两个字：正常"}],
            model=req.config.model,
            temperature=0.0,
            # 新一代思考模型会先花 reasoning token；给小了会只思考不输出
            max_tokens=512,
        )
    except Exception as e:
        log.warning("连通性测试失败：%s", e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"ok": True, "ms": int((time.time() - started) * 1000), "reply": reply[:40]}


# ── 文献库 ──────────────────────────────────────────────────────────────

_parsing: set[str] = set()
_translating: set[str] = set()
_locks: dict[str, asyncio.Lock] = {}
# 后台记忆写手的强引用：task -> paper_id，删除论文时要能按论文取消
_bg_tasks: dict[asyncio.Task, str] = {}


@dataclass
class _Claim:
    key: tuple[str, str]
    lock: asyncio.Lock
    users: int = 0


@dataclass
class _ClaimRef:
    claim: _Claim
    acquired: bool = False
    released: bool = False


# 同一个 (paper_id, client_id) 的并发请求串行化；users 归零即移除，避免无限增长
_ask_claims: dict[tuple[str, str], _Claim] = {}


def _public(row: dict) -> dict:
    """对外返回时补上可访问的 URL，前端不用拼路径。"""
    pid = row["id"]
    return {
        **row,
        "thumb_url": f"/api/papers/{pid}/thumb" if store.thumb_path(pid) else None,
        "file_url": f"/api/papers/{pid}/file",
    }


@app.get("/api/folders")
async def list_folders() -> dict:
    return {"folders": store.list_folders()}


@app.post("/api/folders", status_code=201)
async def create_folder(req: FolderIn) -> dict:
    try:
        return store.create_folder(req.name)
    except FolderExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FolderNameError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.patch("/api/folders/{folder_id}")
async def rename_folder(folder_id: str, req: FolderIn) -> dict:
    try:
        folder = store.rename_folder(folder_id, req.name)
    except FolderExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FolderNameError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    return folder


@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str) -> dict:
    result = store.delete_folder(folder_id)
    if result is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    return result


_zotero_syncing = False


@app.get("/api/zotero/web")
async def get_zotero_web() -> dict:
    return zotero_web.public_config(DATA_DIR)


@app.put("/api/zotero/web")
async def put_zotero_web(req: ZoteroWebIn) -> dict:
    current = zotero_web.load_config(DATA_DIR)
    api_key = req.api_key.strip() or str(current.get("api_key") or "")
    library_type = "group" if req.library_type == "group" else "user"
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 Zotero API Key")
    if library_type == "group" and not req.group_id.strip():
        raise HTTPException(status_code=400, detail="群组库需要填写群组 ID")
    config = {
        **current,
        "api_key": api_key,
        "user_id": req.user_id.strip(),
        "library_type": library_type,
        "group_id": req.group_id.strip(),
    }
    try:
        async with zotero_web.ZoteroWebClient(config) as client:
            info = await client.verify()
    except zotero_web.ZoteroWebError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # 用户 ID 以 API Key 返回的账号为准，用户不用自己去查数字 ID。
    reported_user = str(info.get("userID") or "")
    if reported_user:
        config["user_id"] = reported_user
    elif not config["user_id"]:
        raise HTTPException(
            status_code=400,
            detail="无法从 API Key 读取用户 ID，请手动填写用户 ID",
        )
    zotero_web.save_config(DATA_DIR, config)
    return {**zotero_web.public_config(DATA_DIR), "username": info.get("username")}


@app.post("/api/zotero/web/test")
async def test_zotero_web() -> dict:
    config = zotero_web.load_config(DATA_DIR)
    try:
        async with zotero_web.ZoteroWebClient(config) as client:
            info = await client.verify()
    except zotero_web.ZoteroWebError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "ok": True,
        "user_id": info.get("userID"),
        "username": info.get("username"),
        "access": info.get("access"),
    }


@app.post("/api/zotero/web/sync")
async def sync_zotero_web() -> dict:
    global _zotero_syncing
    if _zotero_syncing:
        raise HTTPException(status_code=409, detail="同步正在进行，请稍后再试")
    _zotero_syncing = True
    try:
        return await zotero_web.sync_library(
            DATA_DIR, store, _schedule_parse
        )
    except zotero_web.ZoteroWebError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        _zotero_syncing = False


@app.get("/api/papers")
async def list_papers() -> dict:
    papers = []
    for row in store.list():
        item = _public(row)
        item["annotation_count"] = len(store.list_annotations(row["id"]))
        papers.append(item)
    return {"papers": papers, "mock": MOCK}


@app.post("/api/papers/upload")
async def upload_papers(
    files: list[UploadFile] = File(...),
    folder_id: str | None = Form(default=None),
) -> dict:
    if folder_id and store.get_folder(folder_id) is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")

    added, failed = [], []
    for f in files:
        name = f.filename or "paper.pdf"
        if not name.lower().endswith(".pdf"):
            failed.append({"filename": name, "error": "只支持 PDF"})
            continue
        data = await f.read()
        if len(data) < 1024:
            failed.append({"filename": name, "error": "文件过小，可能不是有效 PDF"})
            continue
        meta = {"folder_id": folder_id} if folder_id else None
        row = store.add(name, data, meta)
        added.append(_public(row))
        # 上传后立刻后台解析（布局 + 图表 + 公式裁图），不阻塞上传响应
        asyncio.create_task(_parse_task(row["id"]))
    return {"added": added, "failed": failed}


@app.post("/api/papers/{paper_id}/attachments")
async def upload_attachment(
    paper_id: str, file: UploadFile = File(...)
) -> dict:
    """问答附件：随问题上传的补充材料，按论文分目录保存。"""
    row = store.get(paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > ATTACHMENT_LIMIT:
        raise HTTPException(status_code=413, detail="附件不能超过 20 MB")
    info = store.save_attachment(
        paper_id, file.filename or "attachment", data, title=row.get("title") or ""
    )
    path = store.attachment_path(paper_id, info["name"])
    chars = len(_attachment_text(path)) if path else 0
    store.annotate_attachment(paper_id, info["name"], textChars=chars)
    info["textChars"] = chars
    return info


@app.get("/api/papers/{paper_id}/attachments")
async def list_attachments(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    return {"items": store.list_attachments(paper_id)}


@app.delete("/api/papers/{paper_id}/attachments/{name}")
async def delete_attachment(paper_id: str, name: str) -> dict:
    if not store.delete_attachment(paper_id, name):
        raise HTTPException(status_code=404, detail="附件不存在")
    return {"deleted": name}


@app.get("/api/papers/{paper_id}/attachments/{name}")
async def get_attachment(paper_id: str, name: str) -> FileResponse:
    path = store.attachment_path(paper_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(path)


@app.post("/api/papers/import")
async def import_paper(req: ImportReq) -> dict:
    """从本机路径导入 PDF（桌面壳的原生“打开 PDF”走这里，避免大文件再走 multipart）。"""
    try:
        path = Path(req.path).expanduser().resolve(strict=True)
    except OSError as e:
        raise HTTPException(status_code=404, detail=f"文件不存在：{e}") from e
    if not path.is_file():
        raise HTTPException(status_code=400, detail="不是文件")
    if path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="只支持 PDF")
    if path.stat().st_size < 1024:
        raise HTTPException(status_code=400, detail="文件过小，可能不是有效 PDF")

    row = store.add(path.name, path.read_bytes())
    asyncio.create_task(_parse_task(row["id"]))
    return {"added": [_public(row)], "failed": []}


@app.delete("/api/papers/{paper_id}")
async def delete_paper(paper_id: str) -> dict:
    if not store.remove(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    # 先停掉这篇论文的记忆写手，避免它在 forget 之后又把聊天文件写回来
    tasks = [task for task, pid in _bg_tasks.items() if pid == paper_id]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    memory_engine.get_store(store.root).forget(paper_id)
    return {"ok": True}


@app.patch("/api/papers/{paper_id}/folder")
async def move_paper_to_folder(paper_id: str, req: PaperFolderIn) -> dict:
    try:
        row = store.move_paper(paper_id, req.folder_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if row is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return _public(row)


@app.get("/api/papers/{paper_id}/file")
async def paper_file(paper_id: str) -> FileResponse:
    path = store.pdf_path(paper_id)
    if not path:
        raise HTTPException(status_code=404, detail="论文不存在")
    return FileResponse(path, media_type="application/pdf", filename=f"{paper_id}.pdf")


@app.get("/api/papers/{paper_id}/thumb")
async def paper_thumb(paper_id: str) -> FileResponse:
    path = store.thumb_path(paper_id)
    if not path:
        raise HTTPException(status_code=404, detail="缩略图不存在")
    return FileResponse(path, media_type="image/png")


@app.get("/api/papers/{paper_id}/page/{page}.png")
async def paper_page(paper_id: str, page: int, width: int = 1400) -> Response:
    """原版 PDF 的单页图。阅读页切到「原版」时按需取，服务端按页缓存。"""
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    data = await asyncio.to_thread(store.render_page, paper_id, page, width)
    if data is None:
        raise HTTPException(status_code=404, detail="这一页不存在")
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/papers/{paper_id}/blocks")
async def paper_blocks(paper_id: str) -> dict:
    row = store.get(paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")
    return {
        "paper_id": paper_id,
        "title": row.get("title", ""),
        "authors": row.get("authors", ""),
        "page_count": row.get("pages", 0),
        "page_sizes": store.page_sizes(paper_id),
        "columns": row.get("columns", 1),
        "glossary": row.get("glossary", {}),
        "translation_lang": row.get("translation_lang") or "",
        "progress": {
            "pct": float(row.get("read_pct") or 0),
            "block_id": row.get("read_block") or "",
            "page": int(row.get("read_page") or 0),
        },
        "blocks": blocks,
    }


@app.get("/api/papers/{paper_id}/guide")
async def get_guide(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    path = store.papers_dir / f"{paper_id}.guide.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="导读还没生成")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    # 旧版导读（内容结构已升级）直接作废，让前端重新生成
    if data.get("version") != GUIDE_VERSION:
        raise HTTPException(status_code=404, detail="导读需要重新生成")
    return data


_guiding: set[str] = set()


class GuideReq(BaseModel):
    config: LLMConfigIn | None = None
    refresh: bool = False


@app.post("/api/papers/{paper_id}/guide")
async def make_guide(paper_id: str, req: GuideReq | None = None) -> dict:
    """生成导读卡（概念 + 前置文献 + 阅读路径）。已生成过就直接返回。"""
    row = store.get(paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")

    path = store.papers_dir / f"{paper_id}.guide.json"
    refresh = bool(req and req.refresh)
    if path.exists() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8-sig"))
        if cached.get("version") == GUIDE_VERSION:
            return cached

    if paper_id in _guiding:
        raise HTTPException(status_code=429, detail="导读正在生成中")

    if req and req.config:
        from paperead.llm import set_backend

        set_backend(
            OpenAICompatBackend(
                LLMConfig(
                    api_key=req.config.api_key,
                    base_url=req.config.base_url.rstrip("/"),
                    model=req.config.model,
                    timeout=req.config.timeout,
                )
            )
        )
    elif not env("PAPEREAD_API_KEY"):
        raise HTTPException(status_code=400, detail="未配置模型，无法生成导读")

    _guiding.add(paper_id)
    try:
        objs = [Block(**{**b, "bbox": tuple(b["bbox"])}) for b in blocks]
        guide = await build_guide(objs, row.get("glossary") or {})
        path.write_text(json.dumps(guide, ensure_ascii=False, indent=1), encoding="utf-8")
        store.update(paper_id, guide_status=guide.get("status", "ready"))
        return guide
    finally:
        _guiding.discard(paper_id)


@app.get("/api/papers/{paper_id}/block/{block_id}/provenance")
async def block_provenance(paper_id: str, block_id: str) -> dict:
    """这一段引用了哪些文献、原文怎么说。

    结果按块缓存到 `{paper_id}.prov.json`，同一段只查一次外部接口。
    """
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")

    block = next((b for b in blocks if b.get("id") == block_id), None)
    if not block:
        raise HTTPException(status_code=404, detail="段落不存在")

    cache_path = store.papers_dir / f"{paper_id}.prov.json"
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    if block_id in cache:
        return {"block_id": block_id, "citations": cache[block_id]}

    text = block.get("text") or ""
    cites = find_citations(text)
    if not cites:
        return {"block_id": block_id, "citations": []}

    objs = [Block(**{**b, "bbox": tuple(b["bbox"])}) for b in blocks]
    refs = parse_references(objs)
    resolved = resolve_citations(cites, refs)
    if not resolved:
        return {"block_id": block_id, "citations": []}

    by_index = {r.index: r for r in refs}
    sem = asyncio.Semaphore(4)

    async def one(item: dict) -> dict | None:
        ref = by_index.get(item["ref"]["index"])
        if not ref:
            return None
        async with sem:
            data = await enrich_provenance(ref, text)
        data["marker"] = item["marker"]
        return data

    results = [r for r in await asyncio.gather(*(one(x) for x in resolved[:6])) if r]
    cache[block_id] = results
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"block_id": block_id, "citations": results}


# ── 标注 / 笔记 ──────────────────────────────────────────

ANNOTATION_COLORS = {"yellow", "green", "blue", "pink"}
# highlight = 荧光笔填色；comment = 彩色下划线批注
ANNOTATION_KINDS = {"highlight", "comment"}


class AnnotationIn(BaseModel):
    block_id: str = Field(..., min_length=1, max_length=24)
    source: str = Field("en", pattern="^(en|zh)$")
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    quote: str = Field("", max_length=4000)
    color: str = Field("yellow", max_length=20)
    kind: str = Field("highlight", max_length=20)
    note: str = Field("", max_length=5000)


class AnnotationPatch(BaseModel):
    color: str | None = Field(None, max_length=20)
    note: str | None = Field(None, max_length=5000)
    quote: str | None = Field(None, max_length=4000)
    start: int | None = Field(None, ge=0)
    end: int | None = Field(None, ge=0)


def _annotation_text(blocks: list[dict], block_id: str, source: str) -> str:
    block = next((b for b in blocks if b.get("id") == block_id), None)
    if block is None:
        raise HTTPException(status_code=404, detail="段落不存在")
    text = block.get("text") if source == "en" else (block.get("translated") or "")
    return text or ""


def _check_range(start: int, end: int, text: str) -> None:
    if end <= start:
        raise HTTPException(status_code=400, detail="标注范围无效")
    if end > len(text):
        raise HTTPException(status_code=400, detail="标注范围超出段落长度")


@app.get("/api/papers/{paper_id}/annotations")
async def list_annotations(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    return {"annotations": store.list_annotations(paper_id)}


@app.post("/api/papers/{paper_id}/annotations", status_code=201)
async def add_annotation(paper_id: str, req: AnnotationIn) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id) or []
    text = _annotation_text(blocks, req.block_id, req.source)
    _check_range(req.start, req.end, text)
    color = req.color if req.color in ANNOTATION_COLORS else "yellow"
    kind = req.kind if req.kind in ANNOTATION_KINDS else "highlight"
    return store.add_annotation(
        paper_id,
        {
            "block_id": req.block_id,
            "source": req.source,
            "start": req.start,
            "end": req.end,
            "quote": req.quote,
            "color": color,
            "kind": kind,
            "note": req.note,
        },
    )


@app.patch("/api/papers/{paper_id}/annotations/{annotation_id}")
async def update_annotation(
    paper_id: str, annotation_id: str, req: AnnotationPatch
) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    patch = req.model_dump(exclude_none=True)
    if "color" in patch and patch["color"] not in ANNOTATION_COLORS:
        raise HTTPException(status_code=400, detail="不支持的颜色")
    row = store.update_annotation(paper_id, annotation_id, patch)
    if row is None:
        raise HTTPException(status_code=404, detail="标注不存在")
    return row


@app.delete("/api/papers/{paper_id}/annotations/{annotation_id}")
async def delete_annotation(paper_id: str, annotation_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    if not store.delete_annotation(paper_id, annotation_id):
        raise HTTPException(status_code=404, detail="标注不存在")
    return {"ok": True}


class ProgressIn(BaseModel):
    pct: float = Field(0, ge=0, le=100)
    block_id: str = Field("", max_length=24)
    page: int = Field(1, ge=1)


@app.put("/api/papers/{paper_id}/progress")
async def save_progress(paper_id: str, req: ProgressIn) -> dict:
    """记住读到哪里：只在滚动停稳后由前端低频上报。"""
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    row = store.update(
        paper_id,
        read_pct=round(req.pct, 1),
        read_block=req.block_id,
        read_page=req.page,
        read_at=time.time(),
    )
    return {
        "ok": True,
        "progress": {
            "pct": float((row or {}).get("read_pct") or 0),
            "block_id": (row or {}).get("read_block") or "",
            "page": int((row or {}).get("read_page") or 0),
        },
    }


# ── 自动阅读重点（研究目标 / 方法 / 结论） ───────────────

class HighlightsReq(BaseModel):
    config: LLMConfigIn | None = None
    pages: list[int] = Field(default_factory=list, max_length=12)
    force: bool = False


@app.get("/api/papers/{paper_id}/highlights")
async def get_reading_highlights(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    return {
        "highlights": store.list_highlights(paper_id),
        "pages": store.highlight_pages(paper_id),
        "categories": HIGHLIGHT_CATEGORIES,
    }


@app.post("/api/papers/{paper_id}/highlights")
async def make_reading_highlights(paper_id: str, req: HighlightsReq) -> dict:
    """为已翻译的页补生成阅读重点；已处理过的页默认跳过。"""
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")

    if req.config:
        from paperead.llm import set_backend

        set_backend(
            OpenAICompatBackend(
                LLMConfig(
                    api_key=req.config.api_key,
                    base_url=req.config.base_url.rstrip("/"),
                    model=req.config.model,
                    timeout=req.config.timeout,
                )
            )
        )
        model = req.config.model
    elif env("PAPEREAD_API_KEY"):
        model = env("PAPEREAD_MODEL")
    else:
        raise HTTPException(status_code=400, detail="未配置模型，无法提取阅读重点")

    translated_pages = sorted(
        {
            int(row.get("page") or 0)
            for row in blocks
            if row.get("translatable") and (row.get("translated") or "").strip()
        }
    )
    requested = sorted(set(req.pages)) or translated_pages
    processed: list[int] = []
    added: list[dict] = []
    for page in requested:
        if page not in translated_pages:
            continue
        if not req.force and str(page) in store.highlight_pages(paper_id):
            continue
        items = await _extract_page_highlights(paper_id, page, model=model)
        if items is None:
            # 抽取失败：不记成已处理，下次滚动停稳还会再试
            continue
        processed.append(page)
        added.extend(items)

    return {
        "highlights": added,
        "pages": {
            str(page): store.highlight_pages(paper_id).get(str(page))
            for page in requested
            if str(page) in store.highlight_pages(paper_id)
        },
        "processed": processed,
    }


@app.get("/api/papers/{paper_id}/lineage")
async def get_lineage(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    path = store.papers_dir / f"{paper_id}.lineage.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="脉络还没生成")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    # 旧版结果（引用/文献解析或推断方式已升级）直接作废，让前端重新生成
    if data.get("version") != LINEAGE_VERSION:
        raise HTTPException(status_code=404, detail="脉络需要重新生成")
    return data


_lineaging: set[str] = set()


@app.post("/api/papers/{paper_id}/lineage")
async def make_lineage(paper_id: str, req: GuideReq | None = None) -> dict:
    """生成"思路来源图"：本文的关键想法从哪篇来、改了什么。"""
    row = store.get(paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")

    path = store.papers_dir / f"{paper_id}.lineage.json"
    refresh = bool(req and req.refresh)
    if path.exists() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8-sig"))
        if cached.get("version") == LINEAGE_VERSION:
            return cached
    if paper_id in _lineaging:
        raise HTTPException(status_code=429, detail="脉络正在生成中")

    if req and req.config:
        from paperead.llm import set_backend

        set_backend(
            OpenAICompatBackend(
                LLMConfig(
                    api_key=req.config.api_key,
                    base_url=req.config.base_url.rstrip("/"),
                    model=req.config.model,
                    timeout=req.config.timeout,
                )
            )
        )
    elif not env("PAPEREAD_API_KEY"):
        raise HTTPException(status_code=400, detail="未配置模型，无法生成脉络")

    _lineaging.add(paper_id)
    try:
        objs = [Block(**{**b, "bbox": tuple(b["bbox"])}) for b in blocks]
        data = await build_lineage(objs, pdf_path=store.pdf_path(paper_id))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data
    finally:
        _lineaging.discard(paper_id)


class AskReq(BaseModel):
    question: str
    config: LLMConfigIn | None = None
    web: bool = True
    client_id: str | None = None
    attachments: list[str] = Field(default_factory=list)
    # 阅读页点图表/公式旁的小圆圈加入「附录」的块 id
    block_ids: list[str] = Field(default_factory=list)


@dataclass
class _AskPrep:
    context: str
    sources: list[dict]
    used_web: bool


def _require_ask_ready(paper_id: str, question: str) -> None:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    if store.read_blocks(paper_id) is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")
    if not question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")


def _apply_llm_config(config: LLMConfigIn | None) -> None:
    """把前端带来的模型配置注入全局后端；没有配置时要求环境变量兜底。"""
    if config:
        from paperead.llm import set_backend

        try:
            set_backend(
                OpenAICompatBackend(
                    LLMConfig(
                        api_key=config.api_key,
                        base_url=config.base_url.rstrip("/"),
                        model=config.model,
                        timeout=config.timeout,
                    )
                )
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"模型配置有误：{e}") from e
    elif not env("PAPEREAD_API_KEY"):
        raise HTTPException(status_code=400, detail="未配置模型，无法回答")


def _seed_memory(mem: memory_engine.MemoryStore, paper_id: str) -> None:
    """把标题和导读里的领域灌进记忆卡；记忆关闭时 seed 自己会 no-op。"""
    row = store.get(paper_id) or {}
    domains: list[str] = []
    guide_path = store.papers_dir / f"{paper_id}.guide.json"
    if guide_path.exists():
        try:
            guide = json.loads(guide_path.read_text(encoding="utf-8"))
        except Exception:
            guide = None
        if isinstance(guide, dict):
            domains = [d for d in (guide.get("domains") or []) if isinstance(d, str)]
    mem.seed(paper_id, title=row.get("title", ""), domains=domains)


def _cached_answer(user_msg: dict, cached: dict) -> dict:
    """同一个 client_id 重试时，直接回放已落盘的答案。"""
    return {
        "answer": cached.get("text", ""),
        "sources": cached.get("sources") or [],
        "usedWeb": bool(cached.get("used_web")),
        "latency": cached.get("latency", 0.0),
        "user_message_id": user_msg["id"],
        "answer_message_id": cached["id"],
    }


def _claim(paper_id: str, client_id: str | None) -> _ClaimRef | None:
    """登记一次 claim 使用；没有 client_id 时不上锁。"""
    if not client_id:
        return None
    key = (paper_id, client_id)
    entry = _ask_claims.get(key)
    if entry is None:
        entry = _Claim(key=key, lock=asyncio.Lock())
        _ask_claims[key] = entry
    entry.users += 1
    return _ClaimRef(entry)


async def _acquire_claim(ref: _ClaimRef | None) -> None:
    if ref is None:
        return
    try:
        await ref.claim.lock.acquire()
    except BaseException:
        _release_claim(ref)
        raise
    ref.acquired = True


def _release_claim(ref: _ClaimRef | None) -> None:
    """幂等释放：generator finally 与 background 任务可能各调一次。"""
    if ref is None or ref.released:
        return
    ref.released = True
    if ref.acquired:
        ref.claim.lock.release()
    ref.claim.users -= 1
    if ref.claim.users <= 0:
        _ask_claims.pop(ref.claim.key, None)


def _schedule_writer(
    mem: memory_engine.MemoryStore,
    paper_id: str,
    question: str,
    answer: str,
    standalone: str,
) -> None:
    """回答返回后异步整理记忆；MOCK 或关闭记忆时不写。"""
    if MOCK or not memory_engine.enabled():
        return
    task = asyncio.create_task(
        memory_engine.write_memory(
            mem,
            paper_id,
            question,
            answer,
            standalone,
            mem.touched_concepts(standalone),
        )
    )
    _bg_tasks[task] = paper_id
    task.add_done_callback(lambda t: _bg_tasks.pop(t, None))


ATTACH_TEXT_LIMIT = 12_000
ATTACH_TOTAL_LIMIT = 30_000


def _attachment_text(path: Path) -> str:
    """抽取附件文本：PDF / docx 取正文，文本类直接读。失败返回空串。"""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            doc = pymupdf.open(str(path))
            try:
                parts: list[str] = []
                used = 0
                for page in doc:
                    text = page.get_text() or ""
                    parts.append(text)
                    used += len(text)
                    if used >= ATTACH_TEXT_LIMIT:
                        break
                return "\n".join(parts)[:ATTACH_TEXT_LIMIT]
            finally:
                doc.close()
        if suffix == ".docx":
            from docx import Document

            document = Document(str(path))
            return "\n".join(p.text for p in document.paragraphs)[:ATTACH_TEXT_LIMIT]
        if suffix in {
            ".txt",
            ".md",
            ".markdown",
            ".csv",
            ".tsv",
            ".json",
            ".bib",
            ".tex",
            ".yaml",
            ".yml",
            ".log",
            ".py",
            ".r",
        }:
            return path.read_text(encoding="utf-8", errors="replace")[:ATTACH_TEXT_LIMIT]
    except Exception as error:  # 附件抽取失败不应打断问答
        log.warning("读取问答附件失败：%s", error)
    return ""


def _attachment_context(paper_id: str, names: list[str]) -> str:
    blocks: list[str] = []
    total = 0
    for name in names:
        path = store.attachment_path(paper_id, name)
        if path is None:
            continue
        header = f"### 附件：{path.name}（{path.stat().st_size} 字节）"
        text = _attachment_text(path)
        if text.strip():
            body = text[: max(0, ATTACH_TOTAL_LIMIT - total)]
            total += len(body)
            blocks.append(f"{header}\n{body}")
        else:
            blocks.append(
                f"{header}\n（未能抽取文本，可能是图片或扫描件；只能按文件名参考）"
            )
        if total >= ATTACH_TOTAL_LIMIT:
            break
    if not blocks:
        return ""
    return "以下是用户随问题上传的附件内容，请优先结合它们回答：\n\n" + "\n\n".join(
        blocks
    )


def _block_context(paper_id: str, block_ids: list[str]) -> str:
    """把阅读页「附录」里选中的图表/公式拼成问答材料。"""
    if not block_ids:
        return ""
    rows = {b.get("id"): b for b in (store.read_blocks(paper_id) or [])}
    parts: list[str] = []
    for block_id in block_ids[:6]:
        b = rows.get(block_id)
        if not b:
            continue
        kind = b.get("kind")
        page = b.get("page") or 0
        if kind == "equation":
            num = (b.get("eq_number") or "").strip()
            head = f"### 公式{f'（{num}）' if num else ''} · 第 {page} 页"
            tex = (b.get("latex") or "").strip()
            body = f"$$\n{tex}\n$$" if tex else (b.get("text") or "")
        elif kind in ("figure", "table"):
            label = "图" if kind == "figure" else "表"
            caption = (b.get("caption") or "").strip()
            head = f"### {label}{f'：{caption}' if caption else ''} · 第 {page} 页"
            body = f"用户在阅读页选中了这个{label}的截图，图片已在阅读页展示。"
            if kind == "table" and b.get("table_html"):
                body += "该表已识别出结构，可结合表格的行列与数值回答。"
        else:
            continue
        parts.append(f"{head}\n{body}")
    if not parts:
        return ""
    return (
        "以下是用户在阅读时加入「附录」的图表/公式，请优先结合它们回答：\n\n"
        + "\n\n".join(parts)
    )


async def _prepare_ask(
    paper_id: str, req: AskReq, standalone: str, expanded: list[str]
) -> _AskPrep:
    """检索论文、必要时联网，返回拼好的材料与可点击的出处。"""
    blocks = store.read_blocks(paper_id) or []
    objs = [Block(**{**b, "bbox": tuple(b["bbox"])}) for b in blocks]
    docs, en, zh = qa_engine.build_index(objs)

    query = " ".join([standalone, *expanded])
    hits = qa_engine.retrieve(docs, en, zh, query, k=6)
    top_score = hits[0][1] if hits else 0.0

    web_results: list[dict] = []
    if req.web and qa_engine.needs_external(standalone, top_score):
        # 用英文关键词去搜学术内容，中文原问在通用搜索里几乎搜不到论文
        web_query = " ".join(expanded[:6]) if expanded else standalone
        web_results = await web_search.search(web_query, 4)

    # 命中的段落里带的引用，作为补充材料
    references = parse_references(objs)
    refs: dict[int, str] = {}
    for doc, _ in hits[:3]:
        for item in resolve_citations(find_citations(doc.text), references)[:2]:
            refs[item["ref"]["index"]] = item["ref"]["title"]
        if len(refs) >= 4:
            break
    refs = dict(list(refs.items())[:4])

    context = qa_engine.build_context(hits, refs, web_results, docs)
    attachment_block = _attachment_context(paper_id, req.attachments)
    if attachment_block:
        context = f"{context}\n\n{attachment_block}" if context else attachment_block
    block_block = _block_context(paper_id, req.block_ids)
    if block_block:
        context = f"{context}\n\n{block_block}" if context else block_block

    sources = []
    floor = top_score * 0.4
    for doc, score in hits:
        if score <= 0 or score < floor:
            continue
        sources.append(
            {
                "type": "paper",
                "label": f"P{doc.page}",
                "page": doc.page,
                "blockId": doc.block_id,
                "snippet": (doc.translated or doc.text)[:90],
            }
        )
    for idx, title in refs.items():
        sources.append({"type": "ref", "label": f"R{idx}", "index": idx, "title": title})
    for i, w in enumerate(web_results, 1):
        sources.append(
            {"type": "web", "label": f"W{i}", "title": w["title"], "url": w["url"]}
        )

    return _AskPrep(context=context, sources=sources, used_web=bool(web_results))


@app.post("/api/papers/{paper_id}/ask")
async def ask_paper(paper_id: str, req: AskReq) -> dict:
    """论文问答：先论文内检索，必要时升级到网络搜索，答案带出处。"""
    _require_ask_ready(paper_id, req.question)
    _apply_llm_config(req.config)

    claim = _claim(paper_id, req.client_id)
    await _acquire_claim(claim)
    if claim is not None and not store.get(paper_id):
        _release_claim(claim)
        raise HTTPException(status_code=404, detail="论文不存在")
    try:
        t0 = time.time()
        mem = memory_engine.get_store(store.root)
        _seed_memory(mem, paper_id)

        user_msg, cached = mem.begin_question(paper_id, req.question, req.client_id)
        if cached is not None:
            return _cached_answer(user_msg, cached)

        recent = mem.recent_before(paper_id, user_msg["id"], 6)
        standalone, expanded = await qa_engine.rewrite_and_expand(req.question, recent)
        brief = mem.build_brief(paper_id, standalone, exclude_message_id=user_msg["id"])

        prep = await _prepare_ask(paper_id, req, standalone, expanded)
        text = await qa_engine.answer(standalone, prep.context, brief, recent)

        latency = round(time.time() - t0, 1)
        ai_msg = mem.finish_answer(
            paper_id, user_msg["id"], text, prep.sources, prep.used_web, latency
        )
        _schedule_writer(mem, paper_id, req.question, text, standalone)

        return {
            "answer": ai_msg.get("text", text),
            "sources": prep.sources,
            "usedWeb": prep.used_web,
            "latency": latency,
            "user_message_id": user_msg["id"],
            "answer_message_id": ai_msg["id"],
        }
    finally:
        _release_claim(claim)


@app.post("/api/papers/{paper_id}/ask/stream")
async def ask_stream(paper_id: str, req: AskReq) -> StreamingResponse:
    """流式问答：SSE 事件 status → sources → delta×N → done / error。

    检索（含查询改写、联网）在 `status` 之后进行；来源先于正文到达，
    前端可以先渲染出处 chips 再等 token。同一个 client_id 重试时回放已存答案。
    """
    _require_ask_ready(paper_id, req.question)
    _apply_llm_config(req.config)

    claim = _claim(paper_id, req.client_id)
    await _acquire_claim(claim)
    if claim is not None and not store.get(paper_id):
        _release_claim(claim)
        raise HTTPException(status_code=404, detail="论文不存在")
    try:
        mem = memory_engine.get_store(store.root)
        _seed_memory(mem, paper_id)
        user_msg, cached = mem.begin_question(paper_id, req.question, req.client_id)
    except BaseException:
        _release_claim(claim)
        raise

    if cached is not None:
        # 缓存回放不需要一直占着 claim，返回前就释放
        _release_claim(claim)

        async def cached_gen():
            yield _sse({"type": "status", "stage": "retrieving"})
            yield _sse(
                {
                    "type": "sources",
                    "sources": cached.get("sources") or [],
                    "usedWeb": bool(cached.get("used_web")),
                }
            )
            yield _sse({"type": "delta", "text": cached.get("text", "")})
            yield _sse({"type": "done", "latency": cached.get("latency", 0.0)})

        return StreamingResponse(
            cached_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def gen():
        t0 = time.time()
        try:
            yield _sse({"type": "status", "stage": "retrieving"})
            recent = mem.recent_before(paper_id, user_msg["id"], 6)
            standalone, expanded = await qa_engine.rewrite_and_expand(
                req.question, recent
            )
            brief = mem.build_brief(
                paper_id, standalone, exclude_message_id=user_msg["id"]
            )
            prep = await _prepare_ask(paper_id, req, standalone, expanded)
            yield _sse(
                {"type": "sources", "sources": prep.sources, "usedWeb": prep.used_web}
            )

            parts: list[str] = []
            visible = 0
            async for piece in qa_engine.answer_stream(
                standalone, prep.context, brief, recent
            ):
                if not piece:
                    continue
                parts.append(piece)
                visible += len(piece.strip())
                yield _sse({"type": "delta", "text": piece})
            if not visible:
                raise RuntimeError("模型没有返回内容（可能 max_tokens 被 reasoning 耗尽）")

            # 只在完整回答后落盘；出错时只推 error，不留半截回答
            text = "".join(parts)
            latency = round(time.time() - t0, 1)
            mem.finish_answer(
                paper_id, user_msg["id"], text, prep.sources, prep.used_web, latency
            )
            _schedule_writer(mem, paper_id, req.question, text, standalone)

            yield _sse({"type": "done", "latency": latency})
        except asyncio.CancelledError:
            log.info("问答流已取消：%s", paper_id)
            raise
        except Exception as e:
            log.exception("流式问答失败 %s", paper_id)
            yield _sse({"type": "error", "message": str(e)[:300]})
        finally:
            _release_claim(claim)

    background = BackgroundTask(_release_claim, claim) if claim is not None else None
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        background=background,
    )


@app.get("/api/papers/{paper_id}/chat")
async def paper_chat(paper_id: str, before: str | None = None, limit: int = 50) -> dict:
    """历史对话（按轮配对）：刷新或切回来时恢复聊天气泡。"""
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    return memory_engine.get_store(store.root).history(paper_id, before, limit)


@app.get("/api/papers/{paper_id}/asset/{rel:path}")
async def paper_asset(paper_id: str, rel: str) -> FileResponse:
    base = (store.papers_dir / f"{paper_id}_assets").resolve()
    try:
        target = (base / rel).resolve()
        target.relative_to(base)  # 防路径穿越
    except Exception:
        raise HTTPException(status_code=404, detail="资源不存在")
    if not target.exists():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(target)


@app.post("/api/papers/{paper_id}/parse")
async def reparse(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    if paper_id in _parsing:
        return {"ok": True, "status": "parsing"}
    asyncio.create_task(_parse_task(paper_id))
    return {"ok": True, "status": "parsing"}


class TranslateReq(BaseModel):
    config: LLMConfigIn
    pages: list[int] | None = Field(
        default=None, description="只翻这些页；不传则整篇翻译"
    )
    target_lang: str = Field("zh-Hans", description="译文目标语言")
    auto_highlights: bool = Field(
        True, description="翻完一页后是否自动提取研究目标/方法/结论"
    )


TRANSLATION_LANGS = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁體中文",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
}


def _norm_lang(lang: str | None) -> str:
    return lang if lang in TRANSLATION_LANGS else "zh-Hans"


def _target_name(lang: str) -> str:
    return TRANSLATION_LANGS.get(lang, "简体中文")


def _reset_for_language(paper_id: str, lang: str) -> bool:
    """目标语言变了：清空旧译文和术语表，让本次请求从头翻。

    必须在 `_lock_for(paper_id)` 内调用，避免和正在跑的翻译流互相覆盖。
    """
    row = store.get(paper_id) or {}
    if (row.get("translation_lang") or "zh-Hans") == lang:
        return False
    blocks = store.read_blocks(paper_id) or []
    for block in blocks:
        block["translated"] = ""
    store.write_blocks(paper_id, blocks)
    store.update(paper_id, glossary={}, translated=0, translation_lang=lang)
    # 旧译文对应的重点引用也失效了
    store.clear_highlights(paper_id)
    return True


def _page_blocks(paper_id: str, page: int) -> list[Block]:
    rows = store.read_blocks(paper_id) or []
    return [
        Block(**{**row, "bbox": tuple(row["bbox"])})
        for row in rows
        if int(row.get("page") or 0) == int(page)
    ]


_highlighting: set[tuple[str, int]] = set()


async def _extract_page_highlights(
    paper_id: str, page: int, model: str | None = None
) -> list[dict] | None:
    """翻译完一页后，顺带抽取研究目标/方法/结论。

    返回 None 表示「还没准备好或抽取失败」，调用方不要把这页记成已处理；
    返回 [] 表示抽取成功但这一页确实没有可标的重点。
    """
    key = (paper_id, int(page))
    if key in _highlighting:
        return None
    blocks = _page_blocks(paper_id, page)
    if not any(b.translatable and (b.translated or "").strip() for b in blocks):
        return None
    _highlighting.add(key)
    try:
        items = await extract_highlights(blocks, model=model)
        return store.replace_page_highlights(paper_id, page, items)
    except Exception as exc:
        log.warning("阅读重点提取失败 %s p%s：%s", paper_id, page, exc)
        return None
    finally:
        _highlighting.discard(key)


@app.post("/api/papers/{paper_id}/translate")
async def translate(paper_id: str, req: TranslateReq) -> dict:
    """按页翻译（默认整篇）。

    前端阅读页只请求「当前页 + 下一页」，随滚动增量翻译；已在翻译的页会被跳过，
    避免重复计费。
    """
    row = store.get(paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    if store.read_blocks(paper_id) is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")

    pages = sorted({p for p in (req.pages or []) if 1 <= p <= 10_000}) or None
    if pages is not None:
        key = f"{paper_id}:{','.join(map(str, pages))}"
        if key in _translating:
            return {"ok": True, "status": "translating", "blocks": []}
        _translating.add(key)
    else:
        key = f"{paper_id}:*"
        if key in _translating:
            return {"ok": True, "status": "translating", "blocks": []}
        _translating.add(key)

    try:
        blocks = await _translate_pages(paper_id, req.config, pages, req.target_lang)
        translated_pages = sorted({int(b.get("page") or 0) for b in blocks})
        highlights: list[dict] = []
        if req.auto_highlights:
            for page in translated_pages:
                page_highlights = await _extract_page_highlights(
                    paper_id, page, model=req.config.model
                )
                if page_highlights:
                    highlights.extend(page_highlights)
        return {
            "ok": True,
            "status": "done",
            "pages": pages,
            "blocks": blocks,
            "highlights": highlights,
        }
    except Exception as e:
        log.exception("翻译失败 %s", paper_id)
        raise HTTPException(status_code=502, detail=f"翻译失败：{e}") from e
    finally:
        _translating.discard(key)


async def _translate_pages(
    paper_id: str,
    cfg: LLMConfigIn,
    pages: list[int] | None,
    target_lang: str = "zh-Hans",
) -> list[dict]:
    """翻译指定页的块，返回这些块的最新数据。"""
    lang = _norm_lang(target_lang)
    async with _lock_for(paper_id):
        _reset_for_language(paper_id, lang)
        all_blocks = store.read_blocks(paper_id) or []
        targets = [
            (i, b) for i, b in enumerate(all_blocks)
            if (pages is None or b.get("page") in pages)
            and b.get("translatable")
            and (b.get("text") or "").strip()
            and not b.get("translated")
        ]
        if not targets:
            return []

        objs = [Block(**{**b, "bbox": tuple(b["bbox"])}) for _, b in targets]

        backend = OpenAICompatBackend(
            LLMConfig(
                api_key=cfg.api_key,
                base_url=cfg.base_url.rstrip("/"),
                model=cfg.model,
                timeout=cfg.timeout,
            )
        )
        from paperead.llm import set_backend

        set_backend(backend)

        row = store.get(paper_id) or {}
        glossary = row.get("glossary") or {}
        if not glossary:
            # 术语表对整篇扫一次就够，缓存下来给后续每次按页翻译复用
            everything = [Block(**{**b, "bbox": tuple(b["bbox"])}) for b in all_blocks]
            glossary = await build_glossary(everything, target_name=_target_name(lang))

        await translate_paper(
            objs,
            glossary,
            concurrency=8,
            target_name=_target_name(lang),
            target_lang=lang,
        )

        for (idx, _), obj in zip(targets, objs):
            all_blocks[idx] = obj.to_dict()

        store.write_blocks(paper_id, all_blocks)
        done = sum(1 for b in all_blocks if b.get("translated"))
        store.update(
            paper_id,
            translated=done,
            glossary=glossary,
            translation_lang=lang,
        )
        return [all_blocks[i] for i, _ in targets]


def _lock_for(paper_id: str) -> asyncio.Lock:
    lock = _locks.get(paper_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[paper_id] = lock
    return lock


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/papers/{paper_id}/translate/stream")
async def translate_stream(paper_id: str, req: TranslateReq) -> StreamingResponse:
    """逐块流式翻译：每翻完一段立刻推给前端，首段通常 2–3 秒就能出现。

    事件序列：
        {"type":"start","pages":[1,2],"total":18}
        {"type":"block","block":{...}}     × N
        {"type":"done","translated":18}
        {"type":"error","message":"..."}   （出错时）
    """
    row = store.get(paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="论文不存在")
    if store.read_blocks(paper_id) is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")

    pages = sorted({p for p in (req.pages or []) if 1 <= p <= 10_000}) or None
    key = f"{paper_id}:{','.join(map(str, pages or ['*']))}"
    if key in _translating:
        raise HTTPException(status_code=429, detail="这一页正在翻译中")

    backend = OpenAICompatBackend(
        LLMConfig(
            api_key=req.config.api_key,
            base_url=req.config.base_url.rstrip("/"),
            model=req.config.model,
            timeout=req.config.timeout,
        )
    )
    from paperead.llm import set_backend

    set_backend(backend)

    async def gen():
        _translating.add(key)
        done = 0
        all_blocks: list[dict] = []
        lang = _norm_lang(req.target_lang)
        target_name = _target_name(lang)
        try:
            async with _lock_for(paper_id):
                if _reset_for_language(paper_id, lang):
                    yield _sse({"type": "reset", "target_lang": lang})
                all_blocks = store.read_blocks(paper_id) or []
                targets = [
                    (i, b) for i, b in enumerate(all_blocks)
                    if (pages is None or b.get("page") in pages)
                    and b.get("translatable")
                    and (b.get("text") or "").strip()
                    and not b.get("translated")
                ]
                if not targets:
                    yield _sse({"type": "done", "translated": 0})
                    return

                yield _sse({"type": "start", "pages": pages, "total": len(targets)})

                objs = [Block(**{**b, "bbox": tuple(b["bbox"])}) for _, b in targets]
                glossary = (store.get(paper_id) or {}).get("glossary") or {}
                if not glossary:
                    everything = [
                        Block(**{**b, "bbox": tuple(b["bbox"])}) for b in all_blocks
                    ]
                    glossary = await build_glossary(
                        everything, target_name=target_name
                    )
                    store.update(paper_id, glossary=glossary)

                sem = asyncio.Semaphore(8)

                async def one(i: int, b: Block) -> tuple[int, Block]:
                    async with sem:
                        prev_tail = objs[i - 1].translated[-120:] if i > 0 else ""
                        next_head = objs[i + 1].text[:120] if i + 1 < len(objs) else ""
                        for attempt in range(2):
                            try:
                                tex = await translate_block(
                                    b, glossary, prev_tail, next_head,
                                    temperature=0.25 if attempt == 0 else 0.15,
                                    target_name=target_name,
                                )
                                if quality_ok(b.text, tex, lang=lang) or attempt == 1:
                                    b.translated = tex
                                    return i, b
                            except Exception as e:
                                log.warning("单段翻译失败：%s", e)
                        return i, b

                tasks = [asyncio.create_task(one(i, b)) for i, b in enumerate(objs)]
                try:
                    for fut in asyncio.as_completed(tasks):
                        i, obj = await fut
                        if not obj.translated:
                            continue
                        idx = targets[i][0]
                        all_blocks[idx] = obj.to_dict()
                        done += 1
                        yield _sse({"type": "block", "block": all_blocks[idx]})
                        # 每块都落盘：客户端中途断开也不会白翻
                        store.write_blocks(paper_id, all_blocks)
                finally:
                    for t in tasks:
                        t.cancel()
                    store.write_blocks(paper_id, all_blocks)
                    store.update(
                        paper_id,
                        translated=sum(1 for b in all_blocks if b.get("translated")),
                        translation_lang=lang,
                    )

                # 这一页翻完，顺手抽研究目标 / 方法 / 结论；事件里带上结果，
                # 前端立刻用不同颜色画到正文并更新「阅读重点」。
                # 用户在设置里关掉自动阅读后，跳过这一步，避免额外模型调用。
                if req.auto_highlights:
                    translated_pages = sorted(
                        {
                            int(all_blocks[idx].get("page") or 0)
                            for idx, _ in targets
                            if (all_blocks[idx].get("translated") or "").strip()
                        }
                    )
                    for page in translated_pages:
                        items = await _extract_page_highlights(
                            paper_id, page, model=req.config.model
                        )
                        if items is None:
                            # 还没准备好或抽取失败：不推事件，前端会在补充接口里重试
                            continue
                        yield _sse(
                            {"type": "highlights", "page": page, "highlights": items}
                        )

            yield _sse({"type": "done", "translated": done})
        except asyncio.CancelledError:
            # 客户端断开：把已完成的部分保存后静默退出
            if all_blocks:
                store.write_blocks(paper_id, all_blocks)
            raise
        except Exception as e:
            log.exception("流式翻译失败 %s", paper_id)
            yield _sse({"type": "error", "message": str(e)[:300]})
        finally:
            _translating.discard(key)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _parse_task(paper_id: str) -> None:
    row = store.get(paper_id)
    pdf = store.pdf_path(paper_id)
    if not row or not pdf or paper_id in _parsing:
        return
    _parsing.add(paper_id)
    store.update(paper_id, status="parsing", error="")
    # 重新解析会换 block id 和 offset，旧的自动重点不再可用
    store.clear_highlights(paper_id)
    assets = store.papers_dir / f"{paper_id}_assets"
    try:
        parsed = await asyncio.to_thread(parse_pdf, pdf, paper_id, assets)
        # 配了识图模型就把显示公式转成 LaTeX；阅读页优先用 KaTeX 矢量渲染，
        # 识别失败或没配置时自动回落到裁图，不影响阅读。
        if _vision_config is not None:
            try:
                stats = await asyncio.wait_for(
                    recognize_equations(
                        parsed.blocks,
                        concurrency=6,
                        backend=OpenAICompatBackend(_vision_config),
                    ),
                    timeout=FORMULA_TIMEOUT,
                )
                log.info("公式识别 %s：%s", paper_id, stats)
            except asyncio.TimeoutError:
                log.warning("公式识别超时（%ss），剩余公式保留裁图", FORMULA_TIMEOUT)
            except Exception:
                log.exception("公式识别失败 %s", paper_id)
        blocks: list[dict] = []
        for b in parsed.blocks:
            d = b.to_dict()
            if d.get("image_path"):
                try:
                    rel = Path(d["image_path"]).resolve().relative_to(assets.resolve())
                    d["image_path"] = f"/api/papers/{paper_id}/asset/{rel.as_posix()}"
                except Exception:
                    d["image_path"] = None
            blocks.append(d)
        store.write_blocks(paper_id, blocks)
        store.update(
            paper_id, status="ready", blocks=len(blocks),
            pages=parsed.page_count, columns=parsed.columns, error="",
            # 重新解析会生成全新的 blocks；旧的已译计数不再成立
            translated=0,
        )
        log.info("解析完成 %s：%d 块", paper_id, len(blocks))
        # 手动上传的 PDF 只有正文；解析完再按 DOI/标题补题录摘要，检索时才能按摘要筛选。
        asyncio.create_task(_abstract_task(paper_id))
    except Exception as e:
        log.exception("解析失败 %s", paper_id)
        store.update(paper_id, status="failed", error=str(e)[:300])
    finally:
        _parsing.discard(paper_id)


async def _abstract_task(paper_id: str) -> None:
    try:
        if await fill_abstract(store, paper_id):
            log.info("已回填摘要 %s", paper_id)
    except Exception:
        log.exception("回填摘要失败 %s", paper_id)


# ── Zotero（本机 Zotero 7+ 本地 API） ──────────────────────────────────

ZOTERO_BASE = env("PAPEREAD_ZOTERO", "http://127.0.0.1:23119/api/users/0").rstrip("/")
# 留空时自动探测（Windows 下先读 Zotero 的 prefs.js，取不到再用 ~/Zotero）
ZOTERO_DATA_DIR = env("PAPEREAD_ZOTERO_DATA", "")
_ZOTERO_HINT = (
    "请打开 Zotero 设置 → 高级，在最上方的「杂项」里勾选"
    "“允许此计算机上的其他应用程序与 Zotero 通讯”，然后重试。"
)


class ZoteroError(Exception):
    """本地 API 连不上，或用户关闭了本地 API。"""


def _zotero_client(timeout: float = 15) -> httpx.AsyncClient:
    # trust_env=False：避免系统代理把 127.0.0.1 的请求也转发出去
    return httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False)


async def _zotero_get(
    client: httpx.AsyncClient, path: str, params: dict | None = None
) -> httpx.Response:
    try:
        r = await client.get(f"{ZOTERO_BASE}{path}", params=params)
    except Exception as e:
        raise ZoteroError(f"连不上 Zotero 本地 API（{e}）。{_ZOTERO_HINT}") from e
    if r.status_code == 403:
        raise ZoteroError(f"Zotero 本地 API 未开启。{_ZOTERO_HINT}")
    if r.status_code >= 400:
        raise ZoteroError(f"Zotero 返回 HTTP {r.status_code}：{path}")
    return r


def _zotero_data_dir() -> Path:
    """Zotero 数据目录：默认 ~/Zotero，自定义目录从 prefs.js 里读。"""
    if ZOTERO_DATA_DIR:
        return Path(ZOTERO_DATA_DIR).expanduser()
    appdata = os.getenv("APPDATA")
    if appdata:
        profiles = Path(appdata) / "Zotero" / "Zotero" / "Profiles"
        for prefs in profiles.glob("*/prefs.js"):
            try:
                text = prefs.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = re.search(
                r'user_pref\("extensions\.zotero\.dataDir",\s*"([^"]+)"\)', text
            )
            if match:
                return Path(match.group(1).replace("\\\\", "\\"))
    return Path.home() / "Zotero"


def _zotero_local_path(url: str) -> Path | None:
    """file:// URL → 本地路径（Windows 的 /C:/... 要剥掉开头斜杠）。"""
    if not url.lower().startswith("file:"):
        return None
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if os.name == "nt":
        path = path.lstrip("/")
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        # UNC 路径：file://server/share/file.pdf
        path = f"//{parsed.netloc}{unquote(parsed.path)}"
    return Path(path)


def _zotero_year(data: dict) -> int | None:
    match = re.search(r"(?:^|\D)(1[89]\d{2}|20\d{2})", str(data.get("date") or ""))
    return int(match.group(1)) if match else None


def _zotero_authors(data: dict) -> str:
    names: list[str] = []
    for creator in data.get("creators") or []:
        if creator.get("creatorType") not in (None, "", "author"):
            continue
        if creator.get("name"):
            names.append(str(creator["name"]))
            continue
        last = str(creator.get("lastName") or "").strip()
        first = str(creator.get("firstName") or "").strip()
        if last or first:
            names.append(" ".join(part for part in (first, last) if part))
    return ", ".join(names[:8])


def _zotero_venue(data: dict) -> str:
    for field in ("publicationTitle", "proceedingsTitle", "bookTitle", "repository"):
        value = str(data.get(field) or "").strip()
        if value:
            return value
    return ""


def _zotero_meta(data: dict) -> dict:
    return {
        "title": str(data.get("title") or "").strip(),
        "authors": _zotero_authors(data),
        "year": _zotero_year(data) or "",
        "doi": str(data.get("DOI") or ""),
        "url": str(data.get("url") or ""),
        "venue": _zotero_venue(data),
    }


def _zotero_pdf_attachment(children: list[dict]) -> dict | None:
    for node in children:
        data = node.get("data") or {}
        if data.get("itemType") != "attachment":
            continue
        name = str(data.get("filename") or data.get("path") or "")
        if data.get("contentType") == "application/pdf" or name.lower().endswith(".pdf"):
            return node
    return None


def _zotero_is_regular(item: dict) -> bool:
    data = item.get("data") or {}
    return data.get("itemType") not in ("attachment", "note", "annotation") and bool(
        item.get("key") or data.get("key")
    )


def _normalize_title(title: object) -> str:
    return re.sub(r"[\s\W_]+", "", str(title or "")).lower()


async def _zotero_children(client: httpx.AsyncClient, key: str) -> list[dict]:
    try:
        r = await client.get(f"{ZOTERO_BASE}/items/{key}/children")
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


async def _zotero_candidates(
    client: httpx.AsyncClient,
    *,
    collection: str = "",
    q: str = "",
    limit: int = 50,
    start: int = 0,
    keys: list[str] | None = None,
) -> tuple[list[tuple[dict, dict | None]], int]:
    """取条目及其 PDF 附件；返回 ([(item, pdf_attachment|None)], total)。"""
    if keys:
        targets: list[dict] = []
        for key in keys:
            try:
                r = await client.get(f"{ZOTERO_BASE}/items/{key}")
                if r.status_code == 200:
                    targets.append(r.json())
            except Exception:
                continue
        total = len(targets)
    else:
        params: dict = {
            "limit": limit,
            "start": start,
            # 排除附件；笔记/标注在本地再过滤（多值 NOT 的语法各版本不一致）
            "itemType": "-attachment",
            "sort": "dateAdded",
            "direction": "desc",
        }
        if q.strip():
            params["q"] = q.strip()
            params["qmode"] = "everything"
        path = f"/collections/{collection}/items" if collection else "/items"
        r = await _zotero_get(client, path, params)
        targets = r.json()
        total = int(r.headers.get("Total-Results") or len(targets))

    targets = [item for item in targets if _zotero_is_regular(item)]
    sem = asyncio.Semaphore(6)

    async def resolve(item: dict) -> tuple[dict, dict | None]:
        key = item.get("key") or (item.get("data") or {}).get("key") or ""
        async with sem:
            children = await _zotero_children(client, key) if key else []
        return item, _zotero_pdf_attachment(children)

    return await asyncio.gather(*(resolve(item) for item in targets)), total


async def _zotero_file_bytes(client: httpx.AsyncClient, att: dict) -> bytes:
    """读取附件 PDF：本地 API 会 302 到 file://，拿不到时直接读 Zotero 存储目录。"""
    key = str(att.get("key") or "")
    data = att.get("data") or {}
    candidates: list[Path] = []

    try:
        r = await client.get(f"{ZOTERO_BASE}/items/{key}/file")
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return r.content
        if r.status_code in (301, 302, 303, 307, 308):
            path = _zotero_local_path(r.headers.get("location", ""))
            if path:
                candidates.append(path)
        if not candidates:
            r2 = await client.get(f"{ZOTERO_BASE}/items/{key}/file/view/url")
            if r2.status_code == 200:
                path = _zotero_local_path(r2.text.strip())
                if path:
                    candidates.append(path)
    except Exception:
        pass

    raw_path = str(data.get("path") or "")
    filename = str(data.get("filename") or "")
    if raw_path.startswith("storage:"):
        candidates.append(
            _zotero_data_dir() / "storage" / key / raw_path.split(":", 1)[1]
        )
    elif raw_path:
        candidates.append(Path(raw_path).expanduser())
    if filename:
        candidates.append(_zotero_data_dir() / "storage" / key / filename)

    for path in candidates:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if blob[:4] == b"%PDF":
            return blob
    raise ZoteroError("Zotero 里找不到这个 PDF（可能只同步了题录、附件未下载到本机）")


@app.get("/api/zotero/probe")
async def zotero_probe() -> dict:
    """兼容旧调用：等同 /api/zotero/status。"""
    return await zotero_status()


@app.get("/api/zotero/status")
async def zotero_status() -> dict:
    """探测本机 Zotero 本地 API 是否可读。"""
    data_dir = _zotero_data_dir()
    try:
        async with _zotero_client(timeout=3) as c:
            r = await c.get(f"{ZOTERO_BASE}/items", params={"limit": 1})
    except Exception as e:
        return {"ok": False, "error": f"连不上本机 Zotero（{e}）。{_ZOTERO_HINT}"}
    if r.status_code == 403:
        return {"ok": False, "status": 403, "error": _ZOTERO_HINT}
    return {
        "ok": r.status_code == 200,
        "status": r.status_code,
        "version": r.headers.get("Zotero-API-Version") or "",
        "data_dir": str(data_dir) if data_dir.exists() else "",
    }


@app.get("/api/zotero/collections")
async def zotero_collections() -> dict:
    try:
        async with _zotero_client() as c:
            r = await _zotero_get(c, "/collections", {"limit": 500})
            rows = r.json()
    except ZoteroError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    collections = []
    for row in rows:
        data = row.get("data") or {}
        collections.append(
            {
                "key": row.get("key") or data.get("key"),
                "name": str(data.get("name") or "未命名分类").strip(),
                "items": int((row.get("meta") or {}).get("numItems") or 0),
            }
        )
    collections.sort(key=lambda item: item["name"].lower())
    return {"collections": collections}


@app.get("/api/zotero/items")
async def zotero_items(
    collection: str = Query("", max_length=40),
    q: str = Query("", max_length=200),
    limit: int = Query(50, ge=1, le=100),
    start: int = Query(0, ge=0),
) -> dict:
    """分页列出 Zotero 条目，并标注 PDF 附件 / 是否已在文献库中。"""
    try:
        async with _zotero_client() as c:
            pairs, total = await _zotero_candidates(
                c,
                collection=collection.strip(),
                q=q,
                limit=limit,
                start=start,
            )
    except ZoteroError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    existing = store.list()
    by_id = {row["id"]: row for row in existing}
    by_source = {
        str(row.get("source_id")): row for row in existing if row.get("source_id")
    }
    by_title: dict[str, str] = {}
    for row in existing:
        key = _normalize_title(row.get("title"))
        if key:
            by_title.setdefault(key, row["id"])

    items = []
    for item, pdf in pairs:
        data = item.get("data") or {}
        key = item.get("key") or data.get("key")
        title = str(data.get("title") or "").strip()
        meta = _zotero_meta(data)
        duplicate = by_source.get(f"zotero:{key}")
        if not duplicate and title:
            duplicate = by_id.get(by_title.get(_normalize_title(title), ""))
        pdf_data = (pdf or {}).get("data") or {}
        items.append(
            {
                "key": key,
                "title": title or "(无标题)",
                "authors": meta["authors"],
                "year": meta["year"] or None,
                "item_type": data.get("itemType") or "",
                "publication": meta["venue"],
                "doi": meta["doi"],
                "url": meta["url"],
                "date_added": data.get("dateAdded") or "",
                "pdf": (
                    {
                        "key": pdf.get("key"),
                        "filename": pdf_data.get("filename")
                        or str(pdf_data.get("path") or "").replace("storage:", ""),
                    }
                    if pdf
                    else None
                ),
                "imported": bool(duplicate),
            }
        )
    return {
        "items": items,
        "total": total,
        "start": start,
        "limit": limit,
        "has_more": start + len(items) < total,
    }


class ZoteroImportReq(BaseModel):
    # keys 为空时按 collection/q/limit 批量挑带 PDF 的条目（兼容旧的一键导入）
    keys: list[str] = Field(default_factory=list, max_length=100)
    collection: str = ""
    q: str = ""
    limit: int = Field(20, ge=1, le=100)


@app.post("/api/zotero/import")
async def zotero_import(req: ZoteroImportReq) -> dict:
    """把选中的 Zotero 条目（带 PDF 附件）导入文献库，并后台解析。"""
    imported, skipped, errors, papers = 0, 0, [], []
    try:
        async with _zotero_client(timeout=60) as c:
            pairs, _ = await _zotero_candidates(
                c,
                collection=req.collection.strip(),
                q=req.q,
                limit=req.limit,
                keys=req.keys or None,
            )
            for item, pdf in pairs:
                data = item.get("data") or {}
                key = item.get("key") or data.get("key")
                title = str(data.get("title") or "").strip() or "(无标题)"
                if not pdf:
                    skipped += 1
                    continue
                if key and store.find_by_source(f"zotero:{key}"):
                    skipped += 1
                    continue
                try:
                    blob = await _zotero_file_bytes(c, pdf)
                    meta = {"source": "zotero", "source_id": f"zotero:{key}"}
                    meta.update(
                        {k: v for k, v in _zotero_meta(data).items() if v}
                    )
                    row = store.add(_zotero_name(data), blob, meta)
                    asyncio.create_task(_parse_task(row["id"]))
                    papers.append(_public(row))
                    imported += 1
                except Exception as e:
                    errors.append(f"{title}: {e}"[:160])
    except ZoteroError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors[:10],
        "papers": papers,
    }


class ZoteroSyncReq(BaseModel):
    collection: str = ""
    limit: int = Field(1000, ge=1, le=5000)


@app.post("/api/zotero/sync")
async def zotero_sync(req: ZoteroSyncReq) -> dict:
    """增量同步：把 Zotero（或指定分类）里所有带 PDF 的条目一次导入。

    已在库中的条目按 source_id 去重，重复执行不会产生副本；
    导入后仍交给后台解析，和逐条导入走同一条链路。
    """
    imported, skipped, scanned = 0, 0, 0
    errors: list[str] = []
    papers: list[dict] = []
    try:
        async with _zotero_client(timeout=120) as c:
            start = 0
            while scanned < req.limit:
                pairs, total = await _zotero_candidates(
                    c,
                    collection=req.collection.strip(),
                    limit=100,
                    start=start,
                )
                if not pairs:
                    break
                scanned += len(pairs)
                for item, pdf in pairs:
                    data = item.get("data") or {}
                    key = item.get("key") or data.get("key")
                    title = str(data.get("title") or "").strip() or "(无标题)"
                    if not pdf:
                        skipped += 1
                        continue
                    if key and store.find_by_source(f"zotero:{key}"):
                        skipped += 1
                        continue
                    try:
                        blob = await _zotero_file_bytes(c, pdf)
                        meta = {"source": "zotero", "source_id": f"zotero:{key}"}
                        meta.update({k: v for k, v in _zotero_meta(data).items() if v})
                        row = store.add(_zotero_name(data), blob, meta)
                        asyncio.create_task(_parse_task(row["id"]))
                        papers.append(_public(row))
                        imported += 1
                    except Exception as e:
                        errors.append(f"{title}: {e}"[:160])
                start += len(pairs)
                if start >= total:
                    break
    except ZoteroError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return {
        "imported": imported,
        "skipped": skipped,
        "scanned": scanned,
        "errors": errors[:10],
        "papers": papers,
    }


def _zotero_name(data: dict) -> str:
    title = str(data.get("title") or "zotero-paper")[:80]
    safe = "".join(ch for ch in title if ch not in '\\/:*?"<>|\r\n\t').strip() or "zotero"
    return f"{safe}.pdf"


# ── 文献检索（外部学术库） ──────────────────────────────────────────────


def _add_scholar_pdf(
    filename: str, data: bytes, meta: dict
) -> dict:
    """检索页导入：落盘 + 建索引 + 立刻后台解析，返回原始行。"""
    row = store.add(filename, data, meta)
    asyncio.create_task(_parse_task(row["id"]))
    return row


app.include_router(build_scholar_router(store, _add_scholar_pdf, _public))

# 研究 agent 的项目/产物/标签工具：与检索工具同一前缀，需在 main 里拿到
# 文献库与「入库并后台解析」的回调，所以放在依赖都就绪之后注册。
app.include_router(
    build_tools_router(research_store, store, _add_scholar_pdf, _public)
)


# ── 模型配置同步（桌面 → 手机） ──────────────────────────────────────────
#
# 手机离开电脑后要能自己调模型翻译/问答，需要拿到同一份服务商配置。
# 桌面端保存设置时把整个 `paperead.models` 推上来，手机连上后拉取覆盖本地。
# 密钥因此会落在数据目录里，仅适用于自己的机器/服务器。

MODEL_SETTINGS_PATH = DATA_DIR / "model-settings.json"


def _read_model_settings() -> dict:
    try:
        raw = MODEL_SETTINGS_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


@app.get("/api/settings/models")
def get_model_settings() -> dict:
    return _read_model_settings()


@app.put("/api/settings/models")
def put_model_settings(payload: dict) -> dict:
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings 必须是对象")
    data = {
        "settings": settings,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    MODEL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, MODEL_SETTINGS_PATH)
    return {"ok": True, "updated_at": data["updated_at"]}


if WEB_DIST.exists():
    # pdf.js 的 worker 是 .mjs；Windows 上 mimetypes 默认不认识 .mjs，会按 text/plain
    # 返回，浏览器拒绝把它当模块加载，导致 PDF 预览与打印失败。
    mimetypes.add_type("text/javascript", ".mjs")
    # 直接托管打包后的前端：python -m server.main 打开 http://localhost:8000 即可
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")


def _setup_logging() -> None:
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            DATA_DIR / "logs" / "server.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    ]
    if not is_frozen():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def main() -> None:
    import uvicorn

    _setup_logging()
    host = bind_host()
    log.info(
        "Paperead %s 启动：http://%s:%d（数据：%s）",
        __version__,
        host,
        PORT,
        DATA_DIR,
    )
    uvicorn.run(app, host=host, port=PORT, log_config=None, access_log=False)


if __name__ == "__main__":
    main()

