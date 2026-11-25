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
import hashlib
import json
import logging
import mimetypes
import os
import re
import socket
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

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
from paperead import llm as llm_mod
from paperead.env import env
from paperead.formula import (
    read_cached_latex,
    recognize_equations,
    recognize_inline_math,
    recognize_latex,
    write_cached_latex,
)
from paperead.highlights import CATEGORIES as HIGHLIGHT_CATEGORIES
from paperead.highlights import extract_highlights
from paperead.layout import Block, parse_pdf

from paperead.llm import LLMConfig, OpenAICompatBackend
from paperead.tutor import build_guide
from paperead.tutor.guide import GUIDE_VERSION
from paperead.tutor import dossier as dossier_engine
from paperead.tutor import memory as memory_engine
from paperead.tutor import qa as qa_engine
from paperead.tutor import web as web_search
from paperead.tutor.agent import AgentTool, ToolOutcome, run_agent
from paperead.tutor.citations import (
    find as find_citations,
    marker_key as citation_marker_key,
    resolve as resolve_citations,
)
from paperead.tutor.lineage import LINEAGE_VERSION, build_lineage
from paperead.tutor.provenance import enrich as enrich_provenance, ref_only as ref_only_provenance
from paperead.tutor.references import Reference, parse as parse_references
from paperead.scholar.daily import (
    configure_cache_dir as configure_scholar_daily_cache,
    prefetch_daily as prefetch_scholar_daily,
)
from paperead.translate import (
    _is_translatable,
    build_glossary,
    quality_ok,
    translate_block,
    translate_text,
)
from paperead.translation_task import (
    ACTIVE_STATUSES,
    FATAL_CODES,
    AdaptiveLimiter,
    FatalTranslationError,
    iter_job_paths,
    new_job,
    public_job,
    published_ids,
    read_job as read_translation_job,
    retry_delay,
    write_job as write_translation_job,
)
from paperead.translation_manifest import (
    build_manifest,
    build_report,
    classify_error,
    coverage_guard,
    read_report,
    report_path,
    write_report,
)
from server.ext_api import build_router as build_ext_router
from server.notes_api import build_router as build_notes_router
from server.notes_catalog import NotesCatalog
from server.notes_mirror import NotesMirror
from server.notes_search import NoteSearch
from server.notes_store import NoteStore
from server.plugin_auth import PluginRegistry, is_extension_origin
from server.feedback_api import API_BASE as FEEDBACK_API_BASE
from server.feedback_api import build_feedback_router
from server.llm_proxy import build_llm_proxy_router
from server.onboarding import OnboardingStore, build_onboarding_router
from server.store import (
    ATTACHMENT_LIMIT,
    FolderExistsError,
    FolderNameError,
    LibraryStore,
    render_thumbnail,
)
from server.scholar_api import build_router as build_scholar_router
from server.scholaread import build_router as build_scholaread_router
from server.sync_package import build_router as build_sync_router
from server import zotero_local, zotero_web
from server import babeldoc_engine
from server import babeldoc_setup
from server import storage as storage_migration
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
    cleanup_previous_data_dir,
    config_dir,
    default_data_dir,
    ensure_data_dir,
    is_frozen,
    load_data_location,
    save_data_location,
    web_dist,
)

log = logging.getLogger("paperead.server")

WEB_DIST = web_dist()

DATA_DIR = ensure_data_dir(default_data_dir())
configure_scholar_daily_cache(DATA_DIR / "cache" / "scholar_daily")
PORT = int(env("PAPEREAD_PORT", "8010"))
TOKEN = env("PAPEREAD_TOKEN", "")
ORIGINS = allowed_origins(PORT)
MOCK = env("PAPEREAD_MOCK") == "1"
# 公式识别的整体时间预算：超时就先让论文可读，剩余公式保留裁图
FORMULA_TIMEOUT = float(env("PAPEREAD_FORMULA_TIMEOUT", "180"))

# 版本更新固定从 Gitee 检查（固定 tag `latest` 的发行版）。
# GitHub/镜像只用于手动下载备份，不再参与应用内的检查与下载。
UPDATE_ENDPOINTS = (
    "https://gitee.com/deng_xiaohui/paperead/releases/download/latest/latest.json",
)
UPDATE_RELEASE_PAGE = "https://gitee.com/deng_xiaohui/paperead/releases"
UPDATE_TTL = 10 * 60
_update_cache: dict = {"checked_at": 0.0, "payload": None}

store = LibraryStore(DATA_DIR)
research_store = ResearchStore(DATA_DIR / "research")
onboarding_store = OnboardingStore(DATA_DIR)
# 启动时恢复「上次中断的解析」的强引用，避免任务被 GC 提前回收
_parse_recovery_tasks: set[asyncio.Task] = set()

# 全文翻译任务：生命周期独立于 HTTP 连接；进程重启后从文件恢复为 paused。
_JOB_CACHE: dict[str, dict] = {}
_JOB_TASKS: dict[str, asyncio.Task] = {}
_MODEL_LIMITER: AdaptiveLimiter | None = None


def _model_limiter() -> AdaptiveLimiter:
    global _MODEL_LIMITER
    if _MODEL_LIMITER is None:
        initial = int(env("PAPEREAD_MODEL_CONCURRENCY", "8"))
        maximum = int(env("PAPEREAD_MODEL_CONCURRENCY_MAX", "12"))
        _MODEL_LIMITER = AdaptiveLimiter(initial, max_limit=maximum)
    return _MODEL_LIMITER


def _has_user_data() -> bool:
    """升级用户已经有文献或研究项目，不再触发新手教程。"""
    return bool(store.list()) or bool(
        research_store.list_projects(include_archived=True)
    )


# Windows 注册表里 .svg 的 Content Type 可能被第三方软件写成 image/svg，
# 浏览器对 <img> 的 SVG 严格校验 MIME，会显示成破图占位。强制注册标准类型。
mimetypes.add_type("image/svg+xml", ".svg")

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """进程启动钩子。

    必须用 lifespan，而不是 `app.add_event_handler`：后者在较新的 FastAPI
    里已经从 FastAPI 实例上移除（0.2.14 打包后 sidecar 启动即
    `AttributeError: 'FastAPI' object has no attribute 'add_event_handler'`）。
    lifespan 是官方稳定入口，不随 Starlette 事件 API 的增删而失效。
    """
    # 上次迁移成功后，旧目录等这次启动再清理；旧库可能有数 GB，
    # 放到后台线程删除，避免重启后界面白等一大段。
    threading.Thread(
        target=cleanup_previous_data_dir,
        name="storage-cleanup",
        daemon=True,
    ).start()
    _schedule_scholar_daily_prefetch()
    # 笔记广场目录在软件启动时就开始构建：缓存过期/首次安装就拉起后台刷新，
    # 用户点开广场时大概率已经就绪，不用再等一次文件树下载与索引预热。
    if env("PAPEREAD_MOCK", "0") != "1":
        try:
            notes_catalog.ensure()
        except Exception as exc:
            log.warning("笔记目录预热失败：%s", exc)
    # 进程重启后把卡在 running 的研究会话标回 idle，避免界面一直显示在运行。
    # 原来挂在研究路由的 startup 事件上，同样依赖 Starlette 的事件 API，
    # 这里一并收进 lifespan。
    try:
        for session in research_store.list_all_sessions():
            if session.get("status") == "running":
                research_store.update_session(str(session["id"]), status="idle")
    except Exception as exc:
        log.warning("恢复研究会话状态失败：%s", exc)
    # 解析任务只存在内存里：进程重启后，卡在 parsing 且没有产物的论文
    # 会永久不可译。这里在启动时重新入队，长文档中断后能自愈。
    try:
        for row in store.list(include_project=True):
            if row.get("status") != "parsing":
                continue
            if store.read_blocks(str(row["id"])):
                continue
            task = asyncio.create_task(_parse_task(str(row["id"])))
            _parse_recovery_tasks.add(task)
            task.add_done_callback(_parse_recovery_tasks.discard)
            log.info("恢复上次中断的解析：%s", row["id"])
    except Exception as exc:
        log.warning("恢复解析任务失败：%s", exc)
    try:
        _recover_translation_jobs()
    except Exception as exc:
        log.warning("恢复翻译任务失败：%s", exc)
    yield


app = FastAPI(title="Paperead", version=__version__, lifespan=_lifespan)

# 安装包不带 pandoc：桌面版首次启动在后台下载一次，之后离线可用。
# 设 PAPEREAD_PANDOC_AUTODOWNLOAD=0 可关闭，测试和开发环境默认不下载。
if is_frozen() and env("PAPEREAD_PANDOC_AUTODOWNLOAD", "1") != "0":
    from server.research import pandoc_setup

    pandoc_setup.start_if_missing()

# BabelDOC 引擎体积大，安装包也不内置：首次启动在后台下载分卷并解压。
# 全程只有 urllib 网络读取，不会创建子进程或 cmd 窗口。
# 设 PAPEREAD_BABELDOC_AUTODOWNLOAD=0 可关闭。
if is_frozen() and env("PAPEREAD_BABELDOC_AUTODOWNLOAD", "1") != "0":
    babeldoc_setup.start_if_missing()

# MathJax 完整引擎要 node 运行时（解压后约 80MB），安装包不内置：首次启动在
# 后台下载只含 node.exe 的精简包，装好后自动注册给公式渲染器，立即生效。
# 全程只有 urllib 网络读取，不会创建子进程或 cmd 窗口。
# 设 PAPEREAD_NODE_AUTODOWNLOAD=0 可关闭。
if is_frozen() and env("PAPEREAD_NODE_AUTODOWNLOAD", "1") != "0":
    from server import node_setup

    node_setup.start_if_missing()

# 本机没有 TeX 引擎时，首启在后台装 MiKTeX 便携版到数据目录：不需要管理员、
# 不改系统 PATH，装好后立即能被探测到。延迟启动，避免与其他依赖抢带宽。
# 设 PAPEREAD_TEX_AUTODOWNLOAD=0 可关闭。
if is_frozen() and env("PAPEREAD_TEX_AUTODOWNLOAD", "1") != "0":
    from server import tex_setup

    tex_setup.start_if_missing()

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
app.include_router(build_sync_router(store, DATA_DIR))
# 问题反馈：截图走仓库内容接口，Issue 走 /repos/{owner}/issues。
app.include_router(
    build_feedback_router(
        DATA_DIR,
        api_base=env("PAPEREAD_FEEDBACK_API", FEEDBACK_API_BASE),
    )
)

# 研究 agent 的工具接口：本机调用，自带 token，不走 /api 的 cookie 鉴权。
app.include_router(
    build_internal_router(
        LibrarySearch(store, DATA_DIR / "research" / "search.db"), research_store
    )
)

# 浏览器插件接口：独立命名空间与作用域 token，不暴露主应用 token。
plugin_registry = PluginRegistry(config_dir() / "plugins.json")
app.include_router(
    build_ext_router(
        store=store,
        registry=plugin_registry,
        parse_task=lambda paper_id: _parse_task(paper_id),
        public=lambda row: _public(row),
    )
)

# 外部论文笔记：独立存储与独立检索库，不混进文献库和论文检索结果。
note_store = NoteStore(DATA_DIR)
notes_catalog = NotesCatalog(DATA_DIR)
notes_mirror = NotesMirror(DATA_DIR)
app.include_router(
    build_notes_router(
        note_store,
        NoteSearch(note_store, DATA_DIR / "notes" / "search.db"),
        paper_store=store,
        parse_task=lambda paper_id: _parse_task(paper_id),
        public=lambda row: _public(row),
        catalog=notes_catalog,
        mirror=notes_mirror,
    )
)


def _supplied_token(request: Request) -> str:
    return (
        request.cookies.get("pr_token")
        or request.headers.get("x-paperead-token")
        or request.headers.get("x-paper-reflow-token")
        or request.query_params.get("pr_token")
        or ""
    )


@app.middleware("http")
async def guard(request: Request, call_next):
    """本地 API 防护：cookie/header token + Origin 校验。

    桌面壳把随机 token 作为 HttpOnly cookie 在首页下发，前端无需改代码；
    壳自己用 X-Paperead-Token 调 health。直接 python -m server.main 时
    TOKEN 为空，鉴权关闭，保持原有开发体验。

    /api/ext/v1 是浏览器插件命名空间：ping/pair 对扩展开放，管理接口仍走
    主 token，业务接口由路由校验插件 Bearer token。
    """
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
        # 研究 agent 用 Authorization: Bearer <token> 调本地 LLM 代理
        if TOKEN and request.headers.get("authorization") != f"Bearer {TOKEN}":
            return JSONResponse({"detail": "unauthorized"}, status_code=403)
    elif ext_path:
        if path in {"/api/ext/v1/ping", "/api/ext/v1/pair"}:
            # 脚本管理器的 GM 请求可能没有 Origin；普通网页 Origin 仍拒绝
            if path.endswith("/pair") and origin and not is_extension_origin(origin):
                return JSONResponse(
                    {"detail": "只允许浏览器扩展或用户脚本配对"}, status_code=403
                )
        elif path in {
            "/api/ext/v1/pair/session",
            "/api/ext/v1/pair/wait",
        } or path.startswith("/api/ext/v1/admin/"):
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
            # 业务接口只认插件 Bearer token；用户脚本可能带页面 Origin，
            # 因此不在这里按 Origin 拒绝，Origin 绑定由 require_plugin 负责。
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
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
    allow_origin_regex=r"^(chrome-extension|moz-extension)://[A-Za-z0-9_-]+$",
    allow_methods=["*"],
    allow_headers=["*"],
    # 翻译任务的 409 用该头区分「引擎未就绪」与普通参数错误，
    # 跨源（vite dev）下必须显式暴露，否则前端只能拿到一句文案。
    expose_headers=["X-Translation-Code"],
    # Chrome 对「扩展/网页 → 127.0.0.1」会先发私有网络预检，
    # 不放行的话浏览器直接报 Failed to fetch。
    allow_private_network=True,
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
    # 阅读页按需识别时带上出处，识别结果会回写到该论文的 blocks.json
    paper_id: str = ""
    block_id: str = ""


class PingReq(BaseModel):
    config: LLMConfigIn

class TranslationJobIn(BaseModel):
    config: LLMConfigIn
    pages: list[int] | None = None
    block_ids: list[str] | None = None
    target_lang: str = "zh-Hans"
    auto_highlights: bool = True
    resume_job_id: str | None = None
    # 正文已译完、只是缺译文 PDF 时，用来触发一次「只出成品」的引擎任务
    rebuild_pdf: bool = False


class TranslationJobResumeIn(BaseModel):
    config: LLMConfigIn




class ImportReq(BaseModel):
    path: str = Field(..., min_length=1, description="本机 PDF 绝对路径")
    folder_id: str | None = None
    # 解析阶段识别公式用的识图模型；不传时用启动时同步到服务端的配置
    vision_config: LLMConfigIn | None = None


class ParseReq(BaseModel):
    """重新解析时可带上当前识图模型，避免服务端还没同步到配置。"""

    vision_config: LLMConfigIn | None = None


class FormulaRecognizeReq(BaseModel):
    """给已有论文补齐公式 LaTeX（不重新解析版面）。"""

    config: LLMConfigIn | None = None
    force: bool = False


def _paper_asset_path(paper_id: str, url: str) -> Path | None:
    """把 blocks.json 里的 /api/.../asset/... 地址还原成本地文件。"""
    prefix = f"/api/papers/{paper_id}/asset/"
    if not url or not url.startswith(prefix):
        return None
    base = (store.papers_dir / f"{paper_id}_assets").resolve()
    try:
        target = (base / unquote(url[len(prefix):])).resolve()
    except Exception:
        return None
    if target != base and base not in target.parents:
        return None
    return target if target.is_file() else None


def _to_llm_config(req: LLMConfigIn | None) -> LLMConfig | None:
    if req is None:
        return None
    return LLMConfig(
        api_key=req.api_key,
        base_url=req.base_url.rstrip("/"),
        model=req.model,
        timeout=req.timeout,
    )


class FolderIn(BaseModel):
    name: str = ""


class ZoteroWebIn(BaseModel):
    api_key: str = ""
    user_id: str = ""
    library_type: str = "user"
    group_id: str = ""


class ZoteroPushIn(BaseModel):
    paper_ids: list[str] = Field(default_factory=list)


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
                client.build_request(
                    "GET",
                    url,
                    headers={**headers, "accept-encoding": "identity"},
                    params=query,
                ),
                stream=True,
            )
        except Exception as e:
            await client.aclose()
            raise HTTPException(status_code=503, detail=f"研究 agent 不可用：{e}") from e

        async def relay():
            try:
                # 事件流不压缩：本机 agent 若启用了 gzip，raw 字节会让前端 ES
                # 解析不出事件（表现为一轮结束却没有内容）。
                async for chunk in upstream.aiter_bytes():
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
    """一张公式裁图 → LaTeX。

    识别结果按图片 sha1 缓存；请求里带 paper_id/block_id 时还会回写该论文的
    blocks.json，下次打开直接走 KaTeX，不再重复调用模型。
    """

    async def persist(latex: str) -> None:
        if latex and req.paper_id and req.block_id:
            await asyncio.to_thread(
                store.update_block_latex, req.paper_id, req.block_id, latex
            )

    if MOCK:
        time.sleep(0.4)  # 模拟网络耗时，方便前端调 loading 态
        await persist(MOCK_LATEX)
        return {"ok": True, "latex": MOCK_LATEX, "ms": 400, "mock": True, "cached": False}

    image = _decode(req.image_base64)
    if len(image) < 200:
        raise HTTPException(status_code=400, detail="图片过小，可能不是有效的公式裁图")

    cached = read_cached_latex(image)
    if cached:
        await persist(cached)
        return {"ok": True, "latex": cached, "ms": 0, "mock": False, "cached": True}

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
    if latex:
        write_cached_latex(image, latex)
        await persist(latex)
    return {"ok": bool(latex), "latex": latex, "ms": ms, "mock": False, "cached": False}


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
            max_tokens=2048,
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


@app.post("/api/zotero/web/push")
async def push_zotero_web(body: ZoteroPushIn) -> dict:
    """只把选中的本地论文推送到 Zotero（空列表表示全部）。"""
    global _zotero_syncing
    if _zotero_syncing:
        raise HTTPException(status_code=409, detail="同步正在进行，请稍后再试")
    _zotero_syncing = True
    try:
        return await zotero_web.sync_library(
            DATA_DIR,
            store,
            _schedule_parse,
            paper_ids=body.paper_ids,
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
    vision_config: str = Form(default=""),
) -> dict:
    if folder_id and store.get_folder(folder_id) is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")

    cfg: LLMConfig | None = None
    if vision_config:
        try:
            cfg = _to_llm_config(LLMConfigIn.model_validate_json(vision_config))
        except Exception:
            log.warning("上传时携带的识图配置无法解析，改用服务端已同步配置")

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
        # 同一份 PDF 在同一文件夹里只保留一份；否则会生成两个副本，
        # 其中一个永远停在旧解析结果（用户打不开新版公式的常见原因）。
        existing = store.find_by_pdf_hash(data, folder_id=folder_id)
        if existing is not None:
            added.append(_public(existing))
            if existing.get("status") != "ready":
                asyncio.create_task(_parse_task(existing["id"], cfg))
            continue
        meta = {"folder_id": folder_id} if folder_id else None
        row = store.add(name, data, meta)
        added.append(_public(row))
        # 上传后立刻后台解析（布局 + 图表 + 公式裁图），不阻塞上传响应
        asyncio.create_task(_parse_task(row["id"], cfg))
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
    if req.folder_id and store.get_folder(req.folder_id) is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")

    data = path.read_bytes()
    existing = store.find_by_pdf_hash(data, folder_id=req.folder_id)
    if existing is not None:
        if existing.get("status") != "ready":
            asyncio.create_task(
                _parse_task(existing["id"], _to_llm_config(req.vision_config))
            )
        return {"added": [_public(existing)], "failed": []}

    meta = {"folder_id": req.folder_id} if req.folder_id else None
    row = store.add(path.name, data, meta)
    asyncio.create_task(_parse_task(row["id"], _to_llm_config(req.vision_config)))
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


def _engine_output_dir(paper_id: str, lang: str) -> Path:
    return DATA_DIR / "papers" / f"{paper_id}_translated" / lang



def _engine_pdf_meta(paper_id: str, lang: str, mode: str = "mono") -> dict | None:
    """BabelDOC 成品 PDF 的登记信息；缺失或语种不符时返回 None。

    引擎产物只与「原文 PDF + 本次译文」绑定，不随 blocks 的阅读流增量翻译
    失效：阅读页补翻几段不应让已生成的整篇译文 PDF 变成 404，否则用户每次
    局部翻译后都得重跑一遍引擎。``mode="dual"`` 优先取双语对照版。
    """
    base = _engine_output_dir(paper_id, lang)
    try:
        meta = json.loads((base / "engine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("provider") != "babeldoc":
        return None
    if meta.get("lang") not in (None, lang):
        return None
    name = str(meta.get("file") or "babeldoc.mono.pdf")
    if mode == "dual" and meta.get("dual_file"):
        name = str(meta["dual_file"])
    path = base / name
    if not path.is_file():
        return None
    return {**meta, "path": path, "mode": "dual" if name == meta.get("dual_file") else "mono"}


def _mark_engine_pdf(paper_id: str, job: dict) -> None:
    """任务收尾时登记引擎成品 PDF；只有成功/部分成功才对外提供。"""
    saved = job.get("_engine_pdf")
    if not saved or job.get("status") not in {"succeeded", "partial"}:
        return
    path = Path(saved)
    if not path.is_file():
        return
    lang = str(job.get("lang") or "zh-Hans")
    base = _engine_output_dir(paper_id, lang)
    meta = {
        "provider": "babeldoc",
        "file": path.name,
        "lang": lang,
        "pages": int(job.get("_engine_pages") or 0),
        "built_at": int(time.time()),
    }
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "engine.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("登记引擎译文 PDF 失败：%s", exc)


@app.get("/api/papers/{paper_id}/translation/manifest")
async def translation_manifest(paper_id: str, lang: str = "zh-Hans") -> dict:
    """可译清单：前端用它判断「还差几块」，不再自己推算 pending。"""
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")
    target = _norm_lang(lang)
    report = read_report(DATA_DIR, paper_id, target)
    manifest = build_manifest(blocks, paper_id, target, report=report)
    manifest["report"] = report
    return manifest


@app.get("/api/papers/{paper_id}/translation/report")
async def translation_report(paper_id: str, lang: str = "zh-Hans") -> dict:
    """最近一次翻译任务的验收报告（覆盖率、失败清单、分页统计）。"""
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    target = _norm_lang(lang)
    report = read_report(DATA_DIR, paper_id, target)
    if report is None:
        raise HTTPException(status_code=404, detail="还没有翻译报告")
    return report


def _translated_pdf_gate(
    paper_id: str, lang: str, mode: str = "mono"
) -> tuple[bool, str, str, Path]:
    """译文 PDF 是否可交付：只认 BabelDOC 引擎排好版的成品。

    自研页级回写器已下线，交付链路只剩一条来源：引擎产出的 PDF 经
    ``engine.json`` 登记。引擎缺失时报 ``engine_missing``，不再静默退回
    内部排版，避免两套实现互相干扰。
    """
    target = _norm_lang(lang)
    engine = _engine_pdf_meta(paper_id, target, mode)
    if engine is None:
        return (
            False,
            "engine_missing",
            "译文 PDF 需要由全局翻译引擎生成",
            _engine_output_dir(paper_id, target) / "babeldoc.mono.pdf",
        )
    path = engine["path"]
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        return False, "parsing", "论文还在解析中", path
    manifest = build_manifest(
        blocks, paper_id, target, report=read_report(DATA_DIR, paper_id, target)
    )
    allowed, code, message = coverage_guard(manifest)
    if not allowed:
        return False, code, message, path
    return True, "", "", path


def _remember_job(job: dict) -> dict:
    _JOB_CACHE[str(job["job_id"])] = job
    return job


def _job_by_id(job_id: str) -> dict | None:
    job = _JOB_CACHE.get(job_id)
    if job is not None:
        return job
    for path in iter_job_paths(DATA_DIR):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("job_id") == job_id:
            return _remember_job(data)
    return None


def _active_job(paper_id: str, lang: str) -> dict | None:
    candidates = [
        job
        for job in _JOB_CACHE.values()
        if job.get("paper_id") == paper_id
        and job.get("lang") == lang
        and job.get("status") in ACTIVE_STATUSES
    ]
    if not candidates:
        job = read_translation_job(DATA_DIR, paper_id, lang)
        if job is not None:
            _remember_job(job)
            if job.get("status") in ACTIVE_STATUSES:
                candidates = [job]
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("updated_at") or 0))


def _job_blocks_since(job: dict, since: int) -> tuple[list[dict], int]:
    ids = published_ids(job)[max(0, since) :]
    if not ids:
        return [], max(0, since)
    all_blocks = store.read_blocks(str(job["paper_id"])) or []
    by_id = {str(block.get("id")): block for block in all_blocks}
    blocks = [by_id[bid] for bid in ids if bid in by_id]
    return blocks, max(0, since) + len(ids)


def _job_payload(job: dict, since: int = 0) -> dict:
    blocks, next_cursor = _job_blocks_since(job, since)
    payload = public_job(job, blocks=blocks, since=next_cursor)
    payload["next_cursor"] = next_cursor
    all_blocks = store.read_blocks(str(job["paper_id"])) or []
    manifest = build_manifest(
        all_blocks,
        str(job["paper_id"]),
        str(job.get("lang") or "zh-Hans"),
    )
    payload["coverage"] = manifest["coverage"]
    payload["pending"] = manifest["pending"]
    payload["eligible"] = manifest["eligible_total"]
    return payload


def _launch_translation_job(
    job: dict,
    cfg: LLMConfigIn,
    auto_highlights: bool = False,
) -> None:
    job["status"] = "queued"
    job["error"] = None
    job["cancel_requested"] = False
    job["resume_available"] = False
    job["finished_at"] = 0
    write_translation_job(DATA_DIR, job)
    _remember_job(job)
    task = asyncio.create_task(
        _run_translation_job(
            str(job["paper_id"]),
            str(job["job_id"]),
            cfg,
            auto_highlights=auto_highlights,
        )
    )
    _JOB_TASKS[str(job["job_id"])] = task
    task.add_done_callback(lambda _t: _JOB_TASKS.pop(str(job["job_id"]), None))


def _recover_translation_jobs() -> None:
    """进程重启后：running/queued 任务转为 paused，进度保留，等待用户续跑。"""
    for path in iter_job_paths(DATA_DIR):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not job.get("job_id"):
            continue
        if job.get("status") in {"running", "queued"}:
            job["status"] = "paused"
            job["resume_available"] = True
            job["error"] = {
                "code": "interrupted",
                "message": "服务重启，任务已暂停；点击继续即可从断点接着翻译。",
            }
            write_translation_job(DATA_DIR, job)
        _remember_job(job)


_ENGINE_MISSING = "全局翻译引擎尚未就绪：首次使用需要联网下载引擎，下载完成后重试即可。"


@app.post("/api/papers/{paper_id}/translation/jobs")
async def start_translation_job(paper_id: str, req: TranslationJobIn) -> dict:
    """启动（或复用）整篇翻译任务；返回 job_id，后续用轮询/重连接口取进度。"""
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")
    target = _norm_lang(req.target_lang)

    if req.resume_job_id:
        job = _job_by_id(req.resume_job_id)
        if job is None or job.get("paper_id") != paper_id:
            raise HTTPException(status_code=404, detail="翻译任务不存在")
        if job.get("status") not in {"paused", "failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="任务不在可续跑状态")
        if not babeldoc_engine.available(str(job.get("lang") or "zh-Hans")):
            raise HTTPException(
                status_code=409,
                detail=_ENGINE_MISSING,
                headers={"X-Translation-Code": "engine_missing"},
            )
        job["failures"] = []
        job["failed"] = 0
        _launch_translation_job(job, req.config, req.auto_highlights)
        return _job_payload(job)

    existing = _active_job(paper_id, target)
    if existing is not None:
        return _job_payload(existing)

    manifest = build_manifest(blocks, paper_id, target)
    if manifest["eligible_total"] == 0:
        raise HTTPException(status_code=409, detail="本篇没有可翻译的正文块")
    # 全局翻译只走外部引擎：引擎缺失时直接报错，不回退内部实现。
    if not babeldoc_engine.available(target):
        raise HTTPException(
            status_code=409,
            detail=_ENGINE_MISSING,
            headers={"X-Translation-Code": "engine_missing"},
        )
    job = new_job(paper_id, target)
    job["skipped"] = manifest["skipped"]
    job["total"] = manifest["pending"] + manifest["failed"]
    if req.rebuild_pdf:
        job["engine_pdf_only"] = True
    if job["total"] == 0 and not req.rebuild_pdf:
        job["status"] = "succeeded"
        job["finished_at"] = int(time.time())
        job["report"] = read_report(DATA_DIR, paper_id, target)
        write_translation_job(DATA_DIR, job)
        _remember_job(job)
        return _job_payload(job)
    _launch_translation_job(job, req.config, req.auto_highlights)
    return _job_payload(job)


@app.get("/api/papers/{paper_id}/translation/jobs/active")
async def active_translation_job(
    paper_id: str,
    lang: str = "zh-Hans",
    since: int = 0,
) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    job = _active_job(paper_id, _norm_lang(lang))
    if job is None:
        raise HTTPException(status_code=404, detail="没有进行中的翻译任务")
    return _job_payload(job, since=since)


@app.get("/api/papers/{paper_id}/translation/jobs/{job_id}")
async def get_translation_job(
    paper_id: str,
    job_id: str,
    since: int = 0,
) -> dict:
    job = _job_by_id(job_id)
    if job is None or job.get("paper_id") != paper_id:
        raise HTTPException(status_code=404, detail="翻译任务不存在")
    return _job_payload(job, since=since)


@app.post("/api/papers/{paper_id}/translation/jobs/{job_id}/resume")
async def resume_translation_job(
    paper_id: str,
    job_id: str,
    req: TranslationJobResumeIn,
) -> dict:
    job = _job_by_id(job_id)
    if job is None or job.get("paper_id") != paper_id:
        raise HTTPException(status_code=404, detail="翻译任务不存在")
    if job.get("status") not in {"paused", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="任务不在可续跑状态")
    job["failures"] = []
    job["failed"] = 0
    _launch_translation_job(job, req.config)
    return _job_payload(job)


@app.post("/api/papers/{paper_id}/translation/jobs/{job_id}/cancel")
async def cancel_translation_job(paper_id: str, job_id: str) -> dict:
    job = _job_by_id(job_id)
    if job is None or job.get("paper_id") != paper_id:
        raise HTTPException(status_code=404, detail="翻译任务不存在")
    job["cancel_requested"] = True
    if job.get("status") in {"queued", "paused"}:
        job["status"] = "cancelled"
        job["finished_at"] = int(time.time())
        job["resume_available"] = False
    write_translation_job(DATA_DIR, job)
    return _job_payload(job)


@app.head("/api/papers/{paper_id}/translated-pdf")
async def translated_pdf_head(
    paper_id: str, lang: str = "zh-Hans", mode: str = "mono"
) -> Response:
    """前端进入双栏前先 HEAD 一下，避免 pdf.js 去加载不存在的译文 PDF。

    ``mode=dual`` 取双语对照版（左原文右译文）；没有对照版时自动退回单语。
    """
    ready, code, message, _path = _translated_pdf_gate(paper_id, lang, mode)
    if not ready:
        raise HTTPException(
            status_code=404,
            detail=message,
            headers={"X-Translation-Code": code},
        )
    return Response(status_code=204)


@app.get("/api/papers/{paper_id}/translated-pdf")
async def translated_pdf(
    paper_id: str, lang: str = "zh-Hans", mode: str = "mono"
) -> FileResponse:
    ready, code, message, path = _translated_pdf_gate(paper_id, lang, mode)
    if not ready:
        raise HTTPException(
            status_code=404,
            detail=message,
            headers={"X-Translation-Code": code},
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{paper_id}.{lang}.{mode}.pdf",
    )


@app.post("/api/papers/{paper_id}/translated-pdf/build")
async def build_translated_pdf_route(
    paper_id: str,
    lang: str = "zh-Hans",
    mode: str = "mono",
) -> dict:
    """报告译文 PDF 是否已有引擎成品（不再由服务端重排）。

    译文 PDF 唯一来源是全局翻译引擎：正文已译完、只缺成品时，前端用
    ``translation/jobs`` 的 ``rebuild_pdf`` 触发一次引擎任务；这里只核对
    引擎登记，避免再维护第二套排版实现。
    """
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=400, detail="论文尚未解析")
    target = _norm_lang(lang)
    manifest = build_manifest(
        blocks, paper_id, target, report=read_report(DATA_DIR, paper_id, target)
    )
    allowed, code, message = coverage_guard(manifest)
    if not allowed:
        # 0 覆盖时绝不承认「翻译完成」，也不产出仍显示原文的 PDF。
        raise HTTPException(
            status_code=409,
            detail=message,
            headers={"X-Translation-Code": code},
        )
    engine = _engine_pdf_meta(paper_id, target, mode)
    if engine is None:
        raise HTTPException(
            status_code=409,
            detail="译文 PDF 由全局翻译引擎产出，当前还没有成品；请重新发起一次全文翻译。",
            headers={"X-Translation-Code": "engine_missing"},
        )
    return {
        "status": (
            "partial"
            if manifest["coverage"] < 1.0 or manifest["failed"]
            else "succeeded"
        ),
        "engine": "babeldoc",
        "mode": engine.get("mode"),
        "dual": bool(engine.get("dual_file")),
        "pages": int(engine.get("pages") or 0),
        "failed_pages": [],
        "coverage": manifest["coverage"],
        "translation": {
            "eligible": manifest["eligible_total"],
            "translated": manifest["translated"],
            "failed": manifest["failed"],
            "pending": manifest["pending"],
            "skipped": manifest["skipped"],
            "excluded": manifest["excluded"],
        },
    }


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
        objs = [
            Block(**{**b, "bbox": tuple(b.get("bbox") or (0, 0, 0, 0))})
            for b in blocks
        ]
        guide = await build_guide(objs, row.get("glossary") or {})
        path.write_text(json.dumps(guide, ensure_ascii=False, indent=1), encoding="utf-8")
        store.update(paper_id, guide_status=guide.get("status", "ready"))
        return guide
    finally:
        _guiding.discard(paper_id)


# 出处缓存版本：结构或匹配逻辑变化时递增，旧文件自动失效。
PROV_CACHE_VERSION = 3
# 一段正文的引用上限：正常论文远小于这个数，只防异常数据把响应撑爆。
PROV_MAX = 80
# 只为前几条抓摘要（外部接口慢），其余条目直接给标题 + DOI/arXiv 链接。
PROV_SNIPPET_TOP = 6
# 单条摘要抓取预算（秒）：超时就退回 ref_only，不让整段一起等网络。
PROV_TIMEOUT = 2.5


def _prov_sig(block: dict) -> str:
    """块内容签名：重新解析后文本变了，缓存自动失效。"""
    raw = f"{block.get('kind', '')}\n{block.get('text', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@app.get("/api/papers/{paper_id}/block/{block_id}/provenance")
async def block_provenance(paper_id: str, block_id: str) -> dict:
    """这一段引用了哪些文献、原文怎么说。

    返回整段**全部**可解析引用（不再只给前 6 条），并把解析不到的标记
    单独放进 `unresolved`，前端能区分「参考文献里没有」和「接口失败」。
    结果按块缓存到 `{paper_id}.prov2.json`，缓存带版本与块文本签名。
    """
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")

    block = next((b for b in blocks if b.get("id") == block_id), None)
    if not block:
        raise HTTPException(status_code=404, detail="段落不存在")

    cache_path = store.papers_dir / f"{paper_id}.prov2.json"
    cache: dict = {"version": PROV_CACHE_VERSION, "blocks": {}}
    if cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(raw, dict)
                and raw.get("version") == PROV_CACHE_VERSION
                and isinstance(raw.get("blocks"), dict)
            ):
                cache = raw
        except Exception:
            cache = {"version": PROV_CACHE_VERSION, "blocks": {}}

    text = block.get("text") or ""
    sig = _prov_sig(block)
    entry = (cache.get("blocks") or {}).get(block_id)
    if isinstance(entry, dict) and entry.get("sig") == sig:
        return {
            "block_id": block_id,
            "citations": entry.get("citations") or [],
            "unresolved": entry.get("unresolved") or [],
            "truncated": bool(entry.get("truncated")),
        }

    if not text:
        return {"block_id": block_id, "citations": [], "unresolved": [], "truncated": False}

    cites = find_citations(text)
    if not cites:
        return {"block_id": block_id, "citations": [], "unresolved": [], "truncated": False}

    objs = [
            Block(**{**b, "bbox": tuple(b.get("bbox") or (0, 0, 0, 0))})
            for b in blocks
        ]
    refs = parse_references(objs)
    all_resolved = resolve_citations(cites, refs)
    truncated = len(all_resolved) > PROV_MAX
    resolved = all_resolved[:PROV_MAX]

    resolved_markers = {citation_marker_key(item["marker"]) for item in all_resolved}
    resolved_indexes = {item["ref"]["index"] for item in all_resolved}
    unresolved: list[str] = []
    for c in cites:
        # 数字引用按编号逐条判断：`[3, 4, 5]` 里只有 4 缺条时，
        # 不该把整组都当未解析，也不该因为整组文本出现过就全部放过。
        if c.index is not None:
            if c.index in resolved_indexes:
                continue
            marker = f"({c.index})" if c.marker.startswith("(") else f"[{c.index}]"
        else:
            if citation_marker_key(c.marker) in resolved_markers:
                continue
            marker = c.marker
        if marker in unresolved:
            continue
        unresolved.append(marker)

    if not resolved:
        return {
            "block_id": block_id,
            "citations": [],
            "unresolved": unresolved,
            # 全部条目都可能因 PROV_MAX 被截掉，截断标记要如实返回
            "truncated": truncated,
        }

    by_index = {r.index: r for r in refs}
    sem = asyncio.Semaphore(8)

    async def one(rank: int, item: dict) -> dict | None:
        ref = by_index.get(item["ref"]["index"])
        if not ref:
            return None
        marker = item["marker"]
        if rank >= PROV_SNIPPET_TOP:
            return ref_only_provenance(ref, marker)
        async with sem:
            try:
                data = await asyncio.wait_for(enrich_provenance(ref, text), PROV_TIMEOUT)
            except Exception:
                data = ref_only_provenance(ref, marker)
        data["marker"] = marker
        return data

    results = [
        r for r in await asyncio.gather(*(one(i, x) for i, x in enumerate(resolved))) if r
    ]
    cache.setdefault("blocks", {})[block_id] = {
        "sig": sig,
        "citations": results,
        "unresolved": unresolved,
        "truncated": truncated,
    }
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "block_id": block_id,
        "citations": results,
        "unresolved": unresolved,
        "truncated": truncated,
    }


# ── 标注 / 笔记 ──────────────────────────────────────────

ANNOTATION_COLORS = {"yellow", "green", "blue", "pink"}
# highlight = 荧光笔填色；comment = 彩色下划线批注
ANNOTATION_KINDS = {"highlight", "comment"}


class AnnotationPairIn(BaseModel):
    """另一侧（原文 / 译文）的对应片段：同一条标注在两边都画出来。"""

    source: str = Field(..., pattern="^(en|zh)$")
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    quote: str = Field("", max_length=4000)


class PdfRectIn(BaseModel):
    """PDF 页内选中区域，坐标相对页面宽高归一化到 0~1。

    原版 PDF 视图的页图没有重排段落，标注只能锚在页 + 矩形上；
    归一化后换缩放/换宽度仍能画回原位置。
    """

    x: float = Field(..., ge=0, le=1)
    y: float = Field(..., ge=0, le=1)
    w: float = Field(..., gt=0, le=1)
    h: float = Field(..., gt=0, le=1)


class AnnotationIn(BaseModel):
    block_id: str = Field("", max_length=24)
    source: str = Field("en", pattern="^(en|zh)$")
    start: int = Field(0, ge=0)
    end: int = Field(0, ge=0)
    quote: str = Field("", max_length=4000)
    color: str = Field("yellow", max_length=20)
    kind: str = Field("highlight", max_length=20)
    note: str = Field("", max_length=5000)
    pair: AnnotationPairIn | None = None
    # 原版 PDF 标注：page 从 1 起，rects 是页内归一化矩形
    page: int | None = Field(None, ge=1, le=100000)
    rects: list[PdfRectIn] | None = Field(None, max_length=200)


class AnnotationPatch(BaseModel):
    color: str | None = Field(None, max_length=20)
    note: str | None = Field(None, max_length=5000)
    quote: str | None = Field(None, max_length=4000)
    start: int | None = Field(None, ge=0)
    end: int | None = Field(None, ge=0)
    pair: AnnotationPairIn | None = None


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


def _checked_pair(
    blocks: list[dict], block_id: str, pair: AnnotationPairIn, own_source: str
) -> dict | None:
    """校验另一侧片段。译文还没出来时先不保存，翻译完成后由前端补齐。"""
    if pair.source == own_source:
        raise HTTPException(status_code=400, detail="对应片段必须来自另一侧文本")
    text = _annotation_text(blocks, block_id, pair.source)
    if not text:
        return None
    _check_range(pair.start, pair.end, text)
    return pair.model_dump()


@app.get("/api/papers/{paper_id}/annotations")
async def list_annotations(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    return {"annotations": store.list_annotations(paper_id)}


@app.post("/api/papers/{paper_id}/annotations", status_code=201)
async def add_annotation(paper_id: str, req: AnnotationIn) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    color = req.color if req.color in ANNOTATION_COLORS else "yellow"
    kind = req.kind if req.kind in ANNOTATION_KINDS else "highlight"
    blocks = store.read_blocks(paper_id) or []

    # 原版 PDF：锚点是页码 + 归一化矩形，不走段落文本偏移校验
    if req.page is not None:
        rects = [rect.model_dump() for rect in (req.rects or [])]
        if not rects:
            raise HTTPException(status_code=400, detail="PDF 标注缺少选区")
        if not req.quote.strip():
            raise HTTPException(status_code=400, detail="PDF 标注没有选中文字")
        return store.add_annotation(
            paper_id,
            {
                "block_id": "",
                "source": "en",
                "start": 0,
                "end": 0,
                "quote": req.quote,
                "color": color,
                "kind": kind,
                "note": req.note,
                "pair": None,
                "page": req.page,
                "rects": rects,
            },
        )

    if not req.block_id:
        raise HTTPException(status_code=400, detail="标注缺少段落")
    text = _annotation_text(blocks, req.block_id, req.source)
    _check_range(req.start, req.end, text)
    pair = _checked_pair(blocks, req.block_id, req.pair, req.source) if req.pair else None
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
            "pair": pair,
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
    if "pair" in patch:
        rows = store.list_annotations(paper_id)
        row = next((r for r in rows if r.get("id") == annotation_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="标注不存在")
        pair_req = req.pair
        if pair_req is None:
            patch.pop("pair", None)
        else:
            blocks = store.read_blocks(paper_id) or []
            pair = _checked_pair(
                blocks, str(row.get("block_id") or ""), pair_req, str(row.get("source") or "en")
            )
            if pair is None:
                patch.pop("pair", None)
            else:
                patch["pair"] = pair
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
        objs = [
            Block(**{**b, "bbox": tuple(b.get("bbox") or (0, 0, 0, 0))})
            for b in blocks
        ]
        data = await build_lineage(objs, pdf_path=store.pdf_path(paper_id))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data
    finally:
        _lineaging.discard(paper_id)


class AskQuoteIn(BaseModel):
    """阅读页划词「提问」带过来的引用片段。"""

    text: str = ""
    page: int | None = None
    block_id: str | None = None
    source: str = ""


class AskReq(BaseModel):
    question: str
    config: LLMConfigIn | None = None
    web: bool = True
    client_id: str | None = None
    attachments: list[str] = Field(default_factory=list)
    # 阅读页点图表/公式旁的小圆圈加入「附录」的块 id
    block_ids: list[str] = Field(default_factory=list)
    # 阅读页划词后「提问」选中的原文/译文/PDF 片段
    quotes: list[AskQuoteIn] = Field(default_factory=list)


@dataclass
class _AskPrep:
    context: str
    sources: list[dict]
    used_web: bool
    # 联网过程的结构化结果：前端据此显示「搜了什么/搜到几条/为什么没结果」
    web_status: dict
    # 供研究页同款工具卡片渲染的调用记录（paper_search / web_search）
    tools: list[dict]


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


def _web_status_from_sources(
    sources: list[dict], used_web: bool, attempted: bool
) -> dict:
    """缓存回放时没有原始 webStatus，从出处里的 web 条目还原一个等价结构。"""
    web_sources = [s for s in (sources or []) if s.get("type") == "web"]
    engines = sorted({s.get("engine") for s in web_sources if s.get("engine")})
    return {
        "attempted": bool(attempted or used_web),
        "engine": engines[0] if engines else "",
        "engines": engines,
        "count": len(web_sources),
    }


def _cached_answer(user_msg: dict, cached: dict) -> dict:
    """同一个 client_id 重试时，直接回放已落盘的答案。"""
    sources = cached.get("sources") or []
    used_web = bool(cached.get("used_web"))
    return {
        "answer": cached.get("text", ""),
        "sources": sources,
        "usedWeb": used_web,
        "webStatus": _web_status_from_sources(sources, used_web, attempted=used_web),
        "tools": cached.get("tools") or [],
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


def _quote_context(quotes: list[AskQuoteIn]) -> str:
    """把阅读页划词「提问」选中的片段拼成问答材料。"""
    parts: list[str] = []
    for quote in quotes[:6]:
        text = (quote.text or "").strip()
        if not text:
            continue
        where = f" · 第 {quote.page} 页" if quote.page else ""
        kind = "译文" if quote.source == "zh" else "原文"
        if quote.source == "pdf":
            kind = "原版 PDF"
        parts.append(f"### 用户选中的{kind}片段{where}\n> {text}")
    if not parts:
        return ""
    return (
        "以下是用户在阅读页划词后想要提问的片段，请优先结合它们回答：\n\n"
        + "\n\n".join(parts)
    )


# ── 论文档案直供：一次构建、逐轮复用，不再每次重新检索 ──

_dossier_cache: dict[str, tuple[tuple, dossier_engine.Dossier]] = {}
_dossier_lock = threading.Lock()
_DOSSIER_CACHE_LIMIT = 16


def _paper_dossier(paper_id: str) -> dossier_engine.Dossier:
    """按 blocks 文件的 mtime/size 缓存档案；论文重新解析后自动失效。"""
    try:
        stat = store.blocks_path(paper_id).stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = (0, 0)
    cached = _dossier_cache.get(paper_id)
    if cached is not None and cached[0] == signature:
        return cached[1]
    built = dossier_engine.build_dossier(
        paper_id,
        store.get(paper_id) or {},
        store.read_blocks(paper_id) or [],
    )
    with _dossier_lock:
        if len(_dossier_cache) >= _DOSSIER_CACHE_LIMIT:
            _dossier_cache.pop(next(iter(_dossier_cache)))
        _dossier_cache[paper_id] = (signature, built)
    return built


WEB_FETCH_LIMIT = 2
WEB_FETCH_CHARS = 2_400
WEB_FETCH_TIMEOUT = 6.0


async def _fetch_web_bodies(results: list[dict]) -> list[dict]:
    """材料不足时抓取前几条网页正文。

    只作为「档案 + 摘要都不够」时的补充：并发抓取、单篇 6 秒上限，
    失败（CSDN 521 / 知乎 403 等）直接跳过，绝不让联网把回答卡住。
    """
    picked = [
        item
        for item in results
        if (item.get("url") or "").startswith(("http://", "https://"))
    ][:WEB_FETCH_LIMIT]
    if not picked:
        return []

    async def one(item: dict) -> dict | None:
        try:
            got = await asyncio.wait_for(
                web_search.fetch(item["url"], WEB_FETCH_CHARS),
                timeout=WEB_FETCH_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001
            log.info("联网正文抓取跳过 %s：%s", item.get("url"), e)
            return None
        content = (got.get("content") or "").strip()
        if got.get("error") or not content:
            return None
        return {
            "url": item["url"],
            "title": got.get("title") or item.get("title") or item["url"],
            "content": content[:WEB_FETCH_CHARS],
            "truncated": bool(got.get("truncated")),
        }

    out = await asyncio.gather(*(one(item) for item in picked))
    return [item for item in out if item]


async def _prepare_dossier_events(
    paper_id: str,
    req: AskReq,
    standalone: str,
    expanded: list[str],
) -> AsyncIterator[tuple[str, object]]:
    """论文档案直供：整篇材料一次拼好，本地 BM25 只用于出处与重点段落。

    产出 `("tool", {...})`（档案载入 / 联网检索）与最终 `("prep", _AskPrep)`。
    联网不是默认动作：先看档案能不能回答，确实缺信息才按缺口去搜。
    """
    tools: list[dict] = []
    paper_started = time.time()
    yield (
        "tool",
        {
            "type": "tool",
            "phase": "start",
            "callId": "paper_context",
            "name": "paper_context",
            "input": {"question": standalone, "quotes": len(req.quotes)},
            "startedAt": paper_started,
        },
    )

    dossier = _paper_dossier(paper_id)
    quote_texts = [q.text for q in req.quotes if (q.text or "").strip()]
    context, hits = dossier.render(standalone, quote_texts)
    excerpt = "节选模式" in context

    quote_block = dossier_engine.format_quotes(
        [
            {"text": q.text, "page": q.page, "block_id": q.block_id, "source": q.source}
            for q in req.quotes
        ]
    )
    if quote_block:
        context = f"{quote_block}\n\n{context}"

    attachment_block = _attachment_context(paper_id, req.attachments)
    if attachment_block:
        context = f"{context}\n\n{attachment_block}" if context else attachment_block
    block_block = _block_context(paper_id, req.block_ids)
    if block_block:
        context = f"{context}\n\n{block_block}" if context else block_block

    # 出处：引用片段优先，其次是本地定位到的段落与解析出的参考文献
    sources: list[dict] = []
    seen: set[str] = set()
    for quote in req.quotes:
        block_id = quote.block_id or ""
        if not block_id or block_id in seen:
            continue
        chunk = next((c for c in dossier.chunks if c.block_id == block_id), None)
        if chunk is None:
            continue
        seen.add(block_id)
        sources.append(
            {
                "type": "paper",
                "label": f"P{chunk.page}",
                "page": chunk.page,
                "blockId": chunk.block_id,
                "snippet": (chunk.translated or chunk.text)[:90],
            }
        )

    paper_items: list[dict] = []
    for hit in hits:
        if hit.block_id in seen:
            continue
        seen.add(hit.block_id)
        paper_items.append(
            {
                "label": hit.label,
                "page": hit.page,
                "blockId": hit.block_id,
                "snippet": hit.snippet,
            }
        )
        sources.append(
            {
                "type": "paper",
                "label": hit.label,
                "page": hit.page,
                "blockId": hit.block_id,
                "snippet": hit.snippet,
            }
        )

    refs: dict[int, str] = {}
    if hits:
        blocks = store.read_blocks(paper_id) or []
        objs = [
            Block(**{**b, "bbox": tuple(b.get("bbox") or (0, 0, 0, 0))})
            for b in blocks
        ]
        references = parse_references(objs)
        hit_ids = {hit.block_id for hit in hits[:3]}
        for doc in (d for d in objs if d.id in hit_ids):
            for item in resolve_citations(find_citations(doc.text), references)[:2]:
                refs[item["ref"]["index"]] = item["ref"]["title"]
            if len(refs) >= 4:
                break
        refs = dict(list(refs.items())[:4])
        for idx, title in refs.items():
            sources.append({"type": "ref", "label": f"R{idx}", "index": idx, "title": title})
        ref_block = "\n".join(f"[R{idx}] {title}" for idx, title in refs.items())
        context = f"{context}\n\n【命中的参考文献】\n{ref_block}"

    top_score = hits[0].score if hits else 0.0
    if paper_items:
        pages = "/".join(list(dict.fromkeys(str(i["page"]) for i in paper_items))[:4])
        paper_summary = (
            f"已载入全篇 {len(dossier.body)} 段（{'节选覆盖' if excerpt else '全文'}）"
            f" · 定位 {len(paper_items)} 段 · 第 {pages} 页"
        )
    else:
        paper_summary = f"已载入全篇 {len(dossier.body)} 段（{'节选覆盖' if excerpt else '全文'}）"

    paper_tool = {
        "type": "tool",
        "phase": "end",
        "callId": "paper_context",
        "name": "paper_context",
        "status": "done",
        "summary": paper_summary,
        "items": paper_items,
        "meta": {
            "elapsedMs": int((time.time() - paper_started) * 1000),
            "hits": len(paper_items),
            "chunks": len(dossier.body),
            "excerpt": excerpt,
        },
        "startedAt": paper_started,
        "endedAt": time.time(),
    }
    tools.append(paper_tool)
    yield ("tool", paper_tool)

    web_results: list[dict] = []
    web_status = {"attempted": False, "engine": "", "engines": [], "count": 0}
    web_started = time.time()
    web_failed = False
    plan: qa_engine.WebPlan | None = None

    # 联网前先规划：论文档案能否回答？缺什么？检索式按缺口写。
    # 常规问题（档案已覆盖）在这里就结束，不联网、也不产生联网工具卡片。
    if req.web:
        plan = await qa_engine.plan_web_search(
            standalone,
            hits,
            paper_title=dossier.title,
            paper_brief="\n".join(
                [f"章节：{t}" for t, _ in dossier.outline[:12]]
                + [(c.display or "")[:200] for c in dossier.chunks[:6] if c.display]
            ),
            expanded=expanded,
            has_quotes=bool(quote_texts),
        )
        if not (plan and plan.needed):
            web_status["skipped"] = True
            web_status["reason"] = plan.goal or "论文材料已覆盖，未联网"

    if plan and plan.needed:
        web_status["attempted"] = True
        web_status["goal"] = plan.goal
        yield (
            "tool",
            {
                "type": "tool",
                "phase": "start",
                "callId": "web_search",
                "name": "web_search",
                "input": {
                    "query": plan.queries[0] if plan.queries else "",
                    "queries": plan.queries,
                    "goal": plan.goal,
                },
                "startedAt": web_started,
            },
        )
        try:
            web_results = await web_search.search_many(
                plan.queries,
                limit=4,
                total_limit=6,
                academic=True,
                academic_query=plan.academic_query,
            )
            if not web_results and plan.queries:
                web_results = await web_search.search(plan.queries[0], 4)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("联网检索失败：%s", e)
            web_results = []
            web_failed = True

        web_results = web_results[:6]
        engines = sorted({w.get("engine", "") for w in web_results if w.get("engine")})
        web_status["count"] = len(web_results)
        web_status["engines"] = engines
        web_status["engine"] = engines[0] if engines else ""

        web_items = [
            {
                "label": f"W{i}",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": (item.get("content") or "")[:120],
                "engine": item.get("engine", ""),
            }
            for i, item in enumerate(web_results, 1)
        ]
        web_tool = {
            "type": "tool",
            "phase": "end",
            "callId": "web_search",
            "name": "web_search",
            "status": "error" if web_failed and not web_items else "done",
            "summary": (
                f"联网检索 {len(web_items)} 条" if web_items else "未获取到网络结果"
            ),
            "items": web_items,
            "meta": {
                "elapsedMs": int((time.time() - web_started) * 1000),
                "count": len(web_items),
                "engines": engines,
                "goal": plan.goal,
            },
            "startedAt": web_started,
            "endedAt": time.time(),
        }
        tools.append(web_tool)
        yield ("tool", web_tool)
        for i, w in enumerate(web_results, 1):
            sources.append(
                {
                    "type": "web",
                    "label": f"W{i}",
                    "title": w.get("title", ""),
                    "url": w.get("url", ""),
                    "engine": w.get("engine", ""),
                    "snippet": (w.get("content") or "")[:90],
                }
            )

    body_block = ""
    # 只有「规划器确认论文缺信息」时才深读正文，避免常规问题也去抓网页
    insufficient = bool(plan and plan.needed)
    if web_results and insufficient:
        fetch_started = time.time()
        yield (
            "tool",
            {
                "type": "tool",
                "phase": "start",
                "callId": "web_fetch",
                "name": "web_fetch",
                "input": {"count": min(WEB_FETCH_LIMIT, len(web_results))},
                "startedAt": fetch_started,
            },
        )
        fetched = await _fetch_web_bodies(web_results)
        fetch_items = [
            {
                "label": f"W{i}",
                "title": item["title"],
                "url": item["url"],
                "snippet": item["content"][:120],
            }
            for i, item in enumerate(fetched, 1)
        ]
        fetch_tool = {
            "type": "tool",
            "phase": "end",
            "callId": "web_fetch",
            "name": "web_fetch",
            "status": "done" if fetched else "error",
            "summary": (
                f"深读 {len(fetched)} 篇正文" if fetched else "正文抓取失败，继续用搜索摘要"
            ),
            "items": fetch_items,
            "meta": {
                "elapsedMs": int((time.time() - fetch_started) * 1000),
                "count": len(fetched),
            },
            "startedAt": fetch_started,
            "endedAt": time.time(),
        }
        tools.append(fetch_tool)
        yield ("tool", fetch_tool)
        web_status["depth"] = "deep" if fetched else "search"
        web_status["fetched"] = len(fetched)
        if fetched:
            by_url = {item["url"]: item for item in fetched}
            lines: list[str] = []
            for i, item in enumerate(web_results, 1):
                body = by_url.get(item.get("url", ""))
                if body is None:
                    continue
                lines.append(
                    f"[W{i} 正文] {body['title']}（{body['url']}）\n{body['content']}"
                )
            if lines:
                body_block = (
                    "【联网正文补充（论文材料不足时的深读）】\n" + "\n\n".join(lines)
                )

    if web_results:
        # refs 已经在档案材料里单独列过，这里只拼网络结果，避免重复
        web_block = qa_engine.build_context([], None, web_results, None)
    else:
        web_block = ""
    if web_block:
        context = f"{context}\n\n{web_block}" if context else web_block
    if body_block:
        context = f"{context}\n\n{body_block}" if context else body_block

    yield (
        "prep",
        _AskPrep(
            context=context,
            sources=sources,
            used_web=bool(web_results),
            web_status=web_status,
            tools=tools,
        ),
    )


# 旧「检索管道」：每次提问重建索引、用模型改写检索词再检索命中段落。
# 现已由 _prepare_dossier_events 取代，只在排查回归时保留对照，不在请求路径上。
async def _prepare_ask_events_legacy(
    paper_id: str,
    req: AskReq,
    standalone: str,
    expanded: list[str],
    web_task: asyncio.Task | None = None,
) -> AsyncIterator[tuple[str, object]]:
    """检索过程以事件形式产出，供前端渲染成研究页同款的工具卡片。

    产出 `("tool", {...})` 若干次（start / end），最后产出 `("prep", _AskPrep)`。
    工具事件的结构与研究 agent 的 tool.start / tool.end 对齐：
    callId / name / status / input / items / meta。
    """
    tools: list[dict] = []

    paper_started = time.time()
    yield (
        "tool",
        {
            "type": "tool",
            "phase": "start",
            "callId": "paper_search",
            "name": "paper_search",
            "input": {"query": standalone, "keywords": expanded[:8]},
            "startedAt": paper_started,
        },
    )

    blocks = store.read_blocks(paper_id) or []
    objs = [
            Block(**{**b, "bbox": tuple(b.get("bbox") or (0, 0, 0, 0))})
            for b in blocks
        ]
    docs, en, zh = qa_engine.build_index(objs)

    query = " ".join([standalone, *expanded])
    hits = qa_engine.retrieve(docs, en, zh, query, k=6)
    top_score = hits[0][1] if hits else 0.0

    # 命中的段落里带的引用，作为补充材料；也一并放进检索卡片的 OUT 列表
    references = parse_references(objs)
    refs: dict[int, str] = {}
    for doc, _ in hits[:3]:
        for item in resolve_citations(find_citations(doc.text), references)[:2]:
            refs[item["ref"]["index"]] = item["ref"]["title"]
        if len(refs) >= 4:
            break
    refs = dict(list(refs.items())[:4])

    paper_items = [
        {
            "label": f"P{doc.page}",
            "page": doc.page,
            "blockId": doc.block_id,
            "snippet": (doc.translated or doc.text)[:120],
        }
        for doc, score in hits
        if score > 0
    ]
    paper_items.extend(
        {"label": f"R{idx}", "title": title, "snippet": "论文参考文献"}
        for idx, title in refs.items()
    )
    if hits:
        pages = "/".join(
            list(dict.fromkeys(str(doc.page) for doc, score in hits[:6] if score > 0))[:4]
        )
        paper_summary = f"命中 {len(hits)} 段 · 第 {pages} 页" if pages else "论文里没有直接匹配"
    else:
        paper_summary = "论文里没有直接匹配"
    paper_tool = {
        "type": "tool",
        "phase": "end",
        "callId": "paper_search",
        "name": "paper_search",
        "status": "done",
        "summary": paper_summary,
        "items": paper_items,
        "meta": {
            "elapsedMs": int((time.time() - paper_started) * 1000),
            "hits": len(paper_items),
        },
        "startedAt": paper_started,
        "endedAt": time.time(),
    }
    tools.append(paper_tool)
    yield ("tool", paper_tool)

    web_results: list[dict] = []
    web_status = {"attempted": False, "engine": "", "engines": [], "count": 0}
    web_started = time.time()
    web_failed = False
    try:
        if req.web:
            web_status["attempted"] = True
            yield (
                "tool",
                {
                    "type": "tool",
                    "phase": "start",
                    "callId": "web_search",
                    "name": "web_search",
                    "input": {"query": standalone, "keywords": expanded[:6]},
                    "startedAt": web_started,
                },
            )
            try:
                base = await web_task if web_task is not None else await web_search.search(
                    standalone, 4
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("联网检索失败：%s", e)
                base = []
                web_failed = True
            base = base or []
            web_results.extend(base)

            # 论文没直接写、或概念题、或基础结果太少 → 扩大检索
            insufficient = qa_engine.needs_external(standalone, top_score) or not hits
            need_more = insufficient
            if need_more or len(web_results) < 2:
                zh_words = [w for w in expanded if re.search(r"[\u4e00-\u9fff]", w)]
                en_words = [w for w in expanded if not re.search(r"[\u4e00-\u9fff]", w)]
                extra_queries = [standalone]
                # 中文关键词拼成一条更具体的查询；纯英文串在中文搜索里常被拆坏，只给学术源
                if zh_words and zh_words[0] != standalone:
                    extra_queries.append(" ".join(zh_words[:8]))
                en_query = " ".join(en_words[:6])
                extra = await web_search.search_many(
                    extra_queries,
                    limit=4,
                    total_limit=5,
                    academic=need_more or len(web_results) < 2,
                    academic_query=en_query or " ".join(expanded[:6]) or standalone,
                )
                web_results = web_search.dedupe([*base, *extra])

            web_results = web_results[:6]
            engines = sorted({w.get("engine", "") for w in web_results if w.get("engine")})
            web_status["count"] = len(web_results)
            web_status["engines"] = engines
            web_status["engine"] = engines[0] if engines else ""

            web_items = [
                {
                    "label": f"W{i}",
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": (item.get("content") or "")[:120],
                    "engine": item.get("engine", ""),
                }
                for i, item in enumerate(web_results, 1)
            ]
            web_tool = {
                "type": "tool",
                "phase": "end",
                "callId": "web_search",
                "name": "web_search",
                "status": "error" if web_failed and not web_items else "done",
                "summary": (
                    f"联网检索 {len(web_items)} 条" if web_items else "未获取到网络结果"
                ),
                "items": web_items,
                "meta": {
                    "elapsedMs": int((time.time() - web_started) * 1000),
                    "count": len(web_items),
                    "engines": engines,
                },
                "startedAt": web_started,
                "endedAt": time.time(),
            }
            tools.append(web_tool)
            yield ("tool", web_tool)
    finally:
        if web_task is not None and not web_task.done():
            web_task.cancel()


    context = qa_engine.build_context(hits, refs, web_results, docs)
    attachment_block = _attachment_context(paper_id, req.attachments)
    if attachment_block:
        context = f"{context}\n\n{attachment_block}" if context else attachment_block
    block_block = _block_context(paper_id, req.block_ids)
    if block_block:
        context = f"{context}\n\n{block_block}" if context else block_block
    quote_block = _quote_context(req.quotes)
    if quote_block:
        context = f"{quote_block}\n\n{context}" if context else quote_block

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
            {
                "type": "web",
                "label": f"W{i}",
                "title": w["title"],
                "url": w["url"],
                "engine": w.get("engine", ""),
                "snippet": (w.get("content") or "")[:90],
            }
        )

    yield (
        "prep",
        _AskPrep(
            context=context,
            sources=sources,
            used_web=bool(web_results),
            web_status=web_status,
            tools=tools,
        ),
    )


async def _prepare_ask(
    paper_id: str,
    req: AskReq,
    standalone: str,
    expanded: list[str],
) -> _AskPrep:
    """非流式调用：把事件流跑完，只取最终结果。"""
    async for kind, payload in _prepare_dossier_events(
        paper_id, req, standalone, expanded
    ):
        if kind == "prep":
            return payload  # type: ignore[return-value]
    raise RuntimeError("检索过程没有产出结果")



# ── 阅读问答 agent：模型自主调用 paper_search / web_search / web_fetch ──

_TUTOR_TOOLS = [
    AgentTool(
        name="paper_search",
        description=(
            "在用户正在阅读的论文里检索相关段落，返回页码、段落 id 与原文/译文片段。"
            "任何关于这篇论文内容的问题都先用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词或问题，中文、英文均可",
                }
            },
            "required": ["query"],
        },
    ),
    AgentTool(
        name="web_search",
        description=(
            "联网搜索公开网页与学术源。只返回标题、URL 和摘要；"
            "需要原理、公式、推导、细节时继续用 web_fetch 读取具体网页。"
            "只在论文确实没写、或需要论文之外的最新知识时使用；"
            "query 必须是按缺口写好的概念/术语组合，不要照抄用户原句。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索式"},
                "max_results": {
                    "type": "integer",
                    "description": "返回条数，默认 5",
                    "minimum": 1,
                    "maximum": 8,
                },
            },
            "required": ["query"],
        },
    ),
    AgentTool(
        name="web_fetch",
        description=(
            "读取指定网页的正文（转成 Markdown 文本，约 2400 字）。"
            "从 web_search 的结果里挑值得细读的链接；抓不到正文时会返回错误，"
            "此时改用搜索摘要回答或换一个来源。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) 网页地址"}
            },
            "required": ["url"],
        },
    ),
]


def _tutor_agent_system(paper_id: str, req: AskReq, memory: str = "") -> str:
    paper = store.get(paper_id) or {}
    title = paper.get("title") or "（未知标题）"
    authors = (paper.get("authors") or "").strip()
    pages = paper.get("pages") or ""
    meta = title
    if authors:
        meta += f" · {authors}"
    if pages:
        meta += f" · {pages} 页"

    lines = [
        "你是论文助教，可以调用工具检索后再回答。",
        "",
        "工具使用：",
        "1. 关于论文内容的问题，先用 paper_search 在论文里找依据；",
    ]
    if req.web:
        lines += [
            "2. 先用 paper_search 判断论文能否回答；论文能答就不要再联网。",
            "3. 只有论文确实没写、或需要论文之外的最新进展/领域知识时才用 web_search；"
            "query 按缺口写清概念与术语，不要直接照抄用户原句；",
            "4. web_search 只给摘要；需要原理、公式、细节时用 web_fetch 读取具体网页正文；",
            "5. 不要重复搜同一内容；信息足够就停止调用工具并直接回答；最多 6 次工具调用。",
        ]
    else:
        lines += [
            "2. 用户已关闭联网检索，不要请求 web_search / web_fetch；论文材料不足时，"
            "用领域常识回答并标注「（通识）」。",
            "3. 不要重复搜同一内容；信息足够就停止调用工具并直接回答；最多 6 次工具调用。",
        ]
    lines += [
        "",
        "回答规则：",
        "- 中文，简洁、分点、不要客套；",
        "- 论文依据标 [P页码]，网络依据标 [W序号] 或直接给 Markdown 链接，"
        "自己的领域常识标「（通识）」；",
        "- 不要编造论文里没有的页码、数据、实验结论；",
        "- 材料之间冲突时指出差异，不要强行统一；",
        "- 论文确实没写时不要只回一句「论文中没有提到」，用网络结果或常识给出解释。",
        "",
        f"【论文】{meta}",
    ]
    attachment_block = _attachment_context(paper_id, req.attachments)
    if attachment_block:
        lines += ["", attachment_block]
    block_block = _block_context(paper_id, req.block_ids)
    if block_block:
        lines += ["", block_block]
    quote_block = _quote_context(req.quotes)
    if quote_block:
        lines += ["", quote_block]
    if memory:
        lines += ["", "【背景记忆】（只用于调整讲法，不能当作论文事实）", memory]
    return "\n".join(lines)


def _make_tutor_executor(req: AskReq, state: dict):
    """把三个工具接到论文检索、联网搜索与正文抓取。"""

    async def run(name: str, args: dict) -> ToolOutcome:
        if name == "paper_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return ToolOutcome(
                    content="query 不能为空",
                    status="error",
                    error="query 不能为空",
                    summary="参数错误",
                )
            hits = qa_engine.retrieve(state["docs"], state["en"], state["zh"], query, 6)
            chunks: list[str] = []
            items: list[dict] = []
            seen: set[str] = set()
            for doc, score in hits:
                if score <= 0:
                    continue
                label = f"P{doc.page}"
                chunks.append(f"[{label}] {(doc.translated or doc.text)[:800]}")
                if doc.block_id not in seen:
                    seen.add(doc.block_id)
                    item = {
                        "label": label,
                        "page": doc.page,
                        "blockId": doc.block_id,
                        "snippet": (doc.translated or doc.text)[:120],
                    }
                    items.append(item)
                    state["paper_items"].append(item)
            if not chunks:
                return ToolOutcome(
                    content="论文里没有检索到相关段落。",
                    summary="论文里没有直接匹配",
                )
            pages = "/".join(
                list(dict.fromkeys(str(item["page"]) for item in items))[:4]
            )
            return ToolOutcome(
                content="\n\n".join(chunks),
                summary=f"命中 {len(items)} 段 · 第 {pages} 页",
                items=items,
                meta={"hits": len(items)},
            )

        if name == "web_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return ToolOutcome(
                    content="query 不能为空",
                    status="error",
                    error="query 不能为空",
                    summary="参数错误",
                )
            try:
                limit = int(args.get("max_results") or 5)
            except (TypeError, ValueError):
                limit = 5
            limit = max(1, min(limit, 8))

            try:
                results = await web_search.search_many(
                    [query], limit=limit, total_limit=limit
                )
                if len(results) < 2:
                    results = web_search.dedupe(
                        [*results, *(await web_search.search_academic(query, 2))]
                    )
            except Exception as e:  # noqa: BLE001
                log.info("联网检索失败：%s", e)
                return ToolOutcome(
                    content=f"联网检索失败：{e}",
                    status="error",
                    error=str(e)[:200],
                    summary="联网检索失败",
                )
            results = results[:limit]
            if not results:
                return ToolOutcome(
                    content="联网检索没有返回结果。可以换一个检索式，或基于已有材料与常识回答。",
                    summary="未获取到网络结果",
                    meta={"count": 0, "engines": []},
                )

            new_items: list[dict] = []
            for item in results:
                url = (item.get("url") or "").strip()
                if not url or url in state["web_seen"]:
                    continue
                state["web_seen"].add(url)
                state["web"].append(item)
                new_items.append(
                    {
                        "label": f"W{len(state['web'])}",
                        "title": item.get("title", ""),
                        "url": url,
                        "snippet": (item.get("content") or "")[:120],
                        "engine": item.get("engine", ""),
                    }
                )
            if not new_items:
                return ToolOutcome(
                    content="这些结果刚才已经搜过，没有新的来源。",
                    summary="没有新的网络结果",
                )
            text = "\n\n".join(
                f"[{item['label']}] {item['title']}（{item['url']}）— {item['snippet']}"
                for item in new_items
            )
            engines = sorted(
                {item["engine"] for item in new_items if item.get("engine")}
            )
            return ToolOutcome(
                content=text,
                summary=f"{len(new_items)} 条"
                + (f" · {' / '.join(engines)}" if engines else ""),
                items=new_items,
                meta={"count": len(new_items), "engines": engines},
            )

        if name == "web_fetch":
            url = str(args.get("url") or "").strip()
            got = await web_search.fetch(url, 2400)
            title = got.get("title") or url
            if got.get("error"):
                return ToolOutcome(
                    content=(
                        f"抓取失败（{got['error']}）。不要重试同一个地址，"
                        "改用搜索摘要回答，或换一个来源。"
                    ),
                    status="error",
                    error=str(got["error"])[:200],
                    summary=str(got["error"])[:40],
                    items=[{"label": "URL", "title": title, "url": url}],
                )
            body = got.get("content") or ""
            return ToolOutcome(
                content=(
                    f"【网页】{title}\n{url}\n\n{body}"
                    + ("\n…（正文已截断）" if got.get("truncated") else "")
                ),
                summary=f"正文 {len(body)} 字",
                items=[
                    {
                        "label": "URL",
                        "title": title,
                        "url": url,
                        "snippet": body[:120],
                    }
                ],
                meta={"chars": len(body), "statusCode": got.get("statusCode")},
            )

        return ToolOutcome(
            content=f"未知工具：{name}",
            status="error",
            error="未知工具",
            summary="未知工具",
        )

    return run


async def _ask_agent_events(
    paper_id: str,
    req: AskReq,
    standalone: str,
    recent: list[dict],
    memory: str = "",
) -> AsyncIterator[tuple[str, object]]:
    """产出 `("sse", payload)` 与最后的 `("result", {...})`。"""
    blocks = store.read_blocks(paper_id) or []
    objs = [
            Block(**{**b, "bbox": tuple(b.get("bbox") or (0, 0, 0, 0))})
            for b in blocks
        ]
    docs, en, zh = qa_engine.build_index(objs)
    state = {
        "docs": docs,
        "en": en,
        "zh": zh,
        "web": [],
        "web_seen": set(),
        "paper_items": [],
    }
    executor = _make_tutor_executor(req, state)
    allowed_tools = (
        _TUTOR_TOOLS
        if req.web
        else [tool for tool in _TUTOR_TOOLS if tool.name == "paper_search"]
    )
    history = [
        {
            "role": "user" if m.get("role") == "user" else "assistant",
            "content": m.get("text", ""),
        }
        for m in (recent or [])
    ]

    tools: list[dict] = []
    started: dict[str, float] = {}
    final_text = ""

    async for kind, payload in run_agent(
        system=_tutor_agent_system(paper_id, req, memory),
        question=standalone,
        tools=allowed_tools,
        executor=executor,
        history=history,
    ):
        if kind == "tool_start":
            call_id = str(payload["callId"])  # type: ignore[index]
            started[call_id] = time.time()
            yield (
                "sse",
                {
                    "type": "tool",
                    "phase": "start",
                    "callId": call_id,
                    "name": payload["name"],  # type: ignore[index]
                    "input": payload["input"],  # type: ignore[index]
                    "startedAt": started[call_id],
                },
            )
        elif kind == "tool_end":
            call_id = str(payload["callId"])  # type: ignore[index]
            event = {
                "type": "tool",
                "phase": "end",
                "callId": call_id,
                "name": payload["name"],  # type: ignore[index]
                "status": payload["status"],  # type: ignore[index]
                "summary": payload["summary"],  # type: ignore[index]
                "items": payload.get("items") or [],  # type: ignore[union-attr]
                "error": payload.get("error") or "",  # type: ignore[union-attr]
                "meta": payload.get("meta") or {},  # type: ignore[union-attr]
                "startedAt": started.get(call_id, time.time()),
                "endedAt": time.time(),
            }
            tools.append(event)
            yield ("sse", event)
        elif kind == "text":
            yield ("sse", {"type": "delta", "text": payload})
        elif kind == "final":
            final_text = str(payload or "")

    paper_items: list[dict] = []
    seen_blocks: set[str] = set()
    for item in state["paper_items"]:
        block_id = item.get("blockId") or ""
        if not block_id or block_id in seen_blocks:
            continue
        seen_blocks.add(block_id)
        paper_items.append(
            {
                "type": "paper",
                "label": item["label"],
                "page": item.get("page"),
                "blockId": block_id,
                "snippet": item.get("snippet", ""),
            }
        )
    web_items = state["web"][:6]
    sources = paper_items[:8]
    for i, item in enumerate(web_items, 1):
        sources.append(
            {
                "type": "web",
                "label": f"W{i}",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "engine": item.get("engine", ""),
                "snippet": (item.get("content") or "")[:90],
            }
        )
    engines = sorted({w.get("engine", "") for w in web_items if w.get("engine")})
    web_status = {
        "attempted": bool(web_items),
        "engine": engines[0] if engines else "",
        "engines": engines,
        "count": len(web_items),
    }
    yield (
        "result",
        {
            "answer": final_text,
            "sources": sources,
            "tools": tools,
            "web_status": web_status,
        },
    )




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

        recent = mem.recent_before(paper_id, user_msg["id"], 40)
        # 联网在档案准备阶段按需触发（先判断缺口再搜），这里不再预跑一次原问题检索
        standalone, expanded = await qa_engine.rewrite_and_expand(req.question, recent)
        brief = mem.build_brief(paper_id, standalone, exclude_message_id=user_msg["id"])

        prep = await _prepare_ask(paper_id, req, standalone, expanded)
        text = await qa_engine.answer(
            standalone,
            prep.context,
            brief,
            recent,
            web_empty=bool(prep.web_status.get("attempted")) and not prep.used_web,
        )

        latency = round(time.time() - t0, 1)
        ai_msg = mem.finish_answer(
            paper_id,
            user_msg["id"],
            text,
            prep.sources,
            prep.used_web,
            latency,
            tools=prep.tools,
        )
        _schedule_writer(mem, paper_id, req.question, text, standalone)

        return {
            "answer": ai_msg.get("text", text),
            "sources": prep.sources,
            "usedWeb": prep.used_web,
            "webStatus": prep.web_status,
            "tools": prep.tools,
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
            yield _sse({"type": "status", "stage": "retrieving", "web": req.web})
            sources = cached.get("sources") or []
            used_web = bool(cached.get("used_web"))
            yield _sse(
                {
                    "type": "sources",
                    "sources": sources,
                    "usedWeb": used_web,
                    "webStatus": _web_status_from_sources(
                        sources, used_web, attempted=used_web
                    ),
                    "tools": cached.get("tools") or [],
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
            yield _sse({"type": "status", "stage": "retrieving", "web": req.web})
            recent = mem.recent_before(paper_id, user_msg["id"], 40)
            # 默认走「论文档案直供」：材料一次拼好，不让模型逐轮检索；
            # 需要模型自主多轮检索时显式设 PAPEREAD_TUTOR_AGENT=1。
            use_agent = (
                env("PAPEREAD_TUTOR_AGENT", "0") != "0" and llm_mod.supports_tools()
            )
            # 联网在档案准备阶段按需触发（先判断缺口再搜），不预跑原问题检索
            standalone, expanded = await qa_engine.rewrite_and_expand(
                req.question, recent
            )
            brief = mem.build_brief(
                paper_id, standalone, exclude_message_id=user_msg["id"]
            )

            # 模型支持 tools 时走 agent：由模型决定搜什么、要不要抓网页正文
            if use_agent:
                emitted = False
                result: dict | None = None
                try:
                    async for kind, payload in _ask_agent_events(
                        paper_id, req, standalone, recent, brief
                    ):
                        if kind == "sse":
                            emitted = True
                            yield _sse(payload)  # type: ignore[arg-type]
                        else:
                            result = payload  # type: ignore[assignment]
                    if result is None:
                        raise RuntimeError("agent 没有产出结果")
                    text = str(result.get("answer") or "").strip()
                    if not text:
                        raise RuntimeError(
                            "模型没有返回内容（可能 max_tokens 被 reasoning 耗尽）"
                        )
                    latency = round(time.time() - t0, 1)
                    sources = result.get("sources") or []
                    web_status = result.get("web_status") or {}
                    yield _sse(
                        {
                            "type": "sources",
                            "sources": sources,
                            "usedWeb": bool(web_status.get("count")),
                            "webStatus": web_status,
                            "tools": result.get("tools") or [],
                        }
                    )
                    mem.finish_answer(
                        paper_id,
                        user_msg["id"],
                        text,
                        sources,
                        bool(web_status.get("count")),
                        latency,
                        tools=result.get("tools") or [],
                    )
                    _schedule_writer(mem, paper_id, req.question, text, standalone)
                    yield _sse({"type": "done", "latency": latency})
                    return
                except Exception as e:  # noqa: BLE001
                    # 已经吐出内容就不能重跑，否则前端会看到两段回答
                    if emitted:
                        raise
                    log.info("agent 工具模式不可用，回退固定检索：%s", e)

            prep: _AskPrep | None = None
            async for kind, payload in _prepare_dossier_events(
                paper_id, req, standalone, expanded
            ):
                if kind == "tool":
                    yield _sse(payload)  # type: ignore[arg-type]
                else:
                    prep = payload  # type: ignore[assignment]

            if prep is None:
                raise RuntimeError("检索过程没有产出结果")

            yield _sse(
                {
                    "type": "sources",
                    "sources": prep.sources,
                    "usedWeb": prep.used_web,
                    "webStatus": prep.web_status,
                    "tools": prep.tools,
                }
            )

            parts: list[str] = []
            visible = 0
            async for piece in qa_engine.answer_stream(
                standalone,
                prep.context,
                brief,
                recent,
                web_empty=bool(prep.web_status.get("attempted")) and not prep.used_web,
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
                paper_id,
                user_msg["id"],
                text,
                prep.sources,
                prep.used_web,
                latency,
                tools=prep.tools,
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
async def reparse(paper_id: str, req: ParseReq | None = None) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    if paper_id in _parsing:
        return {"ok": True, "status": "parsing"}
    cfg = _to_llm_config(req.vision_config) if req else None
    asyncio.create_task(_parse_task(paper_id, cfg))
    return {"ok": True, "status": "parsing"}


@app.post("/api/papers/{paper_id}/formulas/recognize")
async def recognize_formulas(paper_id: str, req: FormulaRecognizeReq) -> dict:
    """把已有论文里还没有 LaTeX 的公式裁图批量送给识图模型。

    不重新解析版面，也不动译文/标注；识别结果按图片 sha1 缓存并回写
    blocks.json。阅读页打开论文时若发现公式还是截图，会自动调用一次。
    """
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    cfg = _to_llm_config(req.config) or _vision_config
    if cfg is None:
        raise HTTPException(
            status_code=400,
            detail="未配置识图模型：请先在 设置 → 模型 里配置识图模型，公式会保留为截图。",
        )

    raw_blocks = store.read_blocks(paper_id) or []
    blocks = raw_blocks.get("blocks") if isinstance(raw_blocks, dict) else raw_blocks
    targets = [
        b for b in (blocks or [])
        if isinstance(b, dict)
        and b.get("kind") == "equation"
        and b.get("image_path")
        and (req.force or not (b.get("latex") or "").strip())
    ]
    if not targets:
        return {"ok": True, "total": 0, "recognized": 0, "failed": 0, "items": []}

    backend = OpenAICompatBackend(cfg)
    sem = asyncio.Semaphore(4)

    async def one(block: dict) -> dict | None:
        path = _paper_asset_path(paper_id, str(block.get("image_path") or ""))
        if path is None:
            return None
        image = await asyncio.to_thread(path.read_bytes)
        tex = read_cached_latex(image)
        if not tex or req.force:
            async with sem:
                try:
                    tex = await recognize_latex(image, model=cfg.model, backend=backend)
                except Exception:
                    log.warning("公式识别失败 %s/%s", paper_id, block.get("id"), exc_info=True)
                    tex = ""
        if not tex:
            return None
        write_cached_latex(image, tex)
        await asyncio.to_thread(store.update_block_latex, paper_id, str(block["id"]), tex)
        return {"block_id": block["id"], "latex": tex}

    results = await asyncio.gather(*(one(b) for b in targets))
    items = [r for r in results if r]
    log.info("补齐公式 LaTeX %s：%d/%d", paper_id, len(items), len(targets))
    return {
        "ok": True,
        "total": len(targets),
        "recognized": len(items),
        "failed": len(targets) - len(items),
        "items": items,
    }


class TranslateReq(BaseModel):
    config: LLMConfigIn
    pages: list[int] | None = Field(
        default=None, description="只翻这些页；不传则整篇翻译"
    )
    block_ids: list[str] | None = None
    target_lang: str = Field("zh-Hans", description="译文目标语言")
    auto_highlights: bool = Field(
        True, description="翻完一页后是否自动提取研究目标/方法/结论"
    )


class TranslateTextReq(BaseModel):
    """划词翻译：只翻选中的一小段文字，不写回论文。"""

    config: LLMConfigIn
    text: str = Field(..., min_length=1, max_length=6000)
    target_lang: str = Field("zh-Hans", description="译文目标语言")


TRANSLATION_LANGS = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁體中文",
    "en": "英语",
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


@app.post("/api/translate/text")
async def translate_text_api(req: TranslateTextReq) -> dict:
    """划词翻译：只翻选中的一小段文字，不写回论文、不影响整篇译文。

    阅读页划选原文后点「翻译」会调这里；桌面端由 sidecar 请求模型，
    手机离线时前端改走本机模型通道，两条路径共用同一套提示词与占位符保护。
    """
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="没有可翻译的文字")
    lang = _norm_lang(req.target_lang)
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
    try:
        out = await translate_text(text, target_name=_target_name(lang))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # 模型侧错误统一转成可读提示，前端直接展示
        log.warning("划词翻译失败：%s", exc)
        raise HTTPException(status_code=502, detail=f"翻译失败：{exc}") from exc
    return {
        "ok": True,
        "translation": out,
        "target_lang": lang,
        "target_name": _target_name(lang),
    }


def _reset_for_language(paper_id: str, lang: str) -> bool:
    """目标语言变了：清空旧译文和术语表，让本次请求从头翻。

    必须在 `_lock_for(paper_id)` 内调用，避免和正在跑的翻译流互相覆盖。
    """
    row = store.get(paper_id) or {}
    if (row.get("translation_lang") or "zh-Hans") == lang:
        return False
    # 切换语种：同篇其他语种的活动任务必须作废，否则它会继续把译文写回
    # 已经被清空的 blocks。
    for old_job in list(_JOB_CACHE.values()):
        if old_job.get("paper_id") != paper_id:
            continue
        if old_job.get("lang") == lang:
            continue
        if old_job.get("status") not in {"running", "queued", "paused"}:
            continue
        old_job["cancel_requested"] = True
        old_job["status"] = "cancelled"
        old_job["resume_available"] = False
        old_job["error"] = {
            "code": "language_changed",
            "message": "目标语言已切换，旧语种任务已取消。",
        }
        try:
            write_translation_job(DATA_DIR, old_job)
        except Exception:
            pass
        task = _JOB_TASKS.get(str(old_job.get("job_id")))
        if task is not None:
            task.cancel()
    blocks = store.read_blocks(paper_id) or []
    for block in blocks:
        block["translated"] = ""
    store.write_blocks(paper_id, blocks)
    store.update(paper_id, glossary={}, translated=0, translation_lang=lang)
    # 旧语种的验收报告一并作废，避免新语种沿用它显示「已完成」。
    try:
        report_path(DATA_DIR, paper_id, lang).unlink(missing_ok=True)
    except Exception:
        pass
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
    active_job = _active_job(paper_id, _norm_lang(req.target_lang))
    if active_job is not None and active_job.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail="整篇翻译任务进行中，完成或暂停后再按页补充翻译",
        )

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
        started_at = time.time()
        failures: dict[int, dict] = {}
        report: dict = {}
        final_manifest: dict = {}
        all_blocks: list[dict] = []
        lang = _norm_lang(req.target_lang)
        target_name = _target_name(lang)

        def snapshot(
            force_status: str | None = None,
            error: dict | None = None,
        ) -> tuple[dict, dict]:
            final = build_manifest(all_blocks, paper_id, lang)
            built = build_report(
                final,
                started_at=started_at,
                new_translated=done,
                failures=list(failures.values()),
                usage={"calls": done + len(failures)},
            )
            if force_status:
                built["status"] = force_status
            if error:
                built["error"] = error
            write_report(DATA_DIR, paper_id, lang, built)
            return final, built

        try:
            async with _lock_for(paper_id):
                if _reset_for_language(paper_id, lang):
                    yield _sse({"type": "reset", "target_lang": lang})
                all_blocks = store.read_blocks(paper_id) or []
                # A previous stream can persist a result after the client disconnects.
                # Return requested cached blocks so a stale reader can recover them.
                if req.block_ids is not None:
                    requested_ids = set(req.block_ids)
                    for b in all_blocks:
                        if (b.get("id") in requested_ids
                                and (pages is None or b.get("page") in pages)
                                and (b.get("translated") or "").strip()):
                            yield _sse({"type": "block", "block": b})

                # 可译口径的唯一来源：参考文献/图内文字/公式碎片不再进入待译队列。
                manifest = build_manifest(all_blocks, paper_id, lang)
                eligible_ids = set(manifest["eligible_ids"])
                targets = [
                    (i, b) for i, b in enumerate(all_blocks)
                    if (pages is None or b.get("page") in pages)
                    and (req.block_ids is None or b.get("id") in req.block_ids)
                    and b.get("id") in eligible_ids
                    and not b.get("translated")
                ]
                if req.block_ids is not None:
                    rank = {block_id: i for i, block_id in enumerate(req.block_ids)}
                    targets.sort(key=lambda item: rank[item[1]["id"]])
                if not targets:
                    final_manifest, report = snapshot()
                    yield _sse({
                        "type": "done",
                        "translated": report.get("translated", 0),
                        "new": 0,
                        "failed": report.get("failed", 0),
                        "skipped": report.get("skipped", 0),
                        "total": report.get("total", 0),
                        "coverage": report.get("coverage", 0.0),
                        "status": report.get("status", "blocked"),
                        "report": report,
                    })
                    return

                yield _sse({
                    "type": "start",
                    "pages": pages,
                    "total": len(targets),
                    "eligible": manifest["eligible_total"],
                    "translated": manifest["translated"],
                    "skipped": manifest["skipped"],
                })

                objs = [Block(**{**b, "bbox": tuple(b["bbox"])}) for _, b in targets]
                glossary = (store.get(paper_id) or {}).get("glossary") or {}
                if not glossary and req.block_ids is None:
                    everything = [
                        Block(**{**b, "bbox": tuple(b["bbox"])}) for b in all_blocks
                    ]
                    glossary = await build_glossary(
                        everything, target_name=target_name
                    )
                    store.update(paper_id, glossary=glossary)

                limiter = _model_limiter()

                async def one(i: int, b: Block) -> tuple[int, Block]:
                    prev_tail = objs[i - 1].translated[-120:] if i > 0 else ""
                    next_head = objs[i + 1].text[:120] if i + 1 < len(objs) else ""
                    last_exc: BaseException | None = None
                    for attempt in range(3):
                        try:
                            # 退避等待不占并发槽位，限流时才能尽快恢复吞吐。
                            async with limiter:
                                tex = await translate_block(
                                    b, glossary, prev_tail, next_head,
                                    temperature=0.25 if attempt == 0 else 0.15,
                                    target_name=target_name,
                                )
                            if quality_ok(b.text, tex, lang=lang) or attempt == 2:
                                b.translated = tex
                                limiter.on_success()
                                return i, b
                        except Exception as e:
                            last_exc = e
                            code, message = classify_error(e)
                            limiter.on_error(code)
                            log.warning("单段翻译失败（%s）：%s", code, e)
                            if attempt < 2:
                                await asyncio.sleep(retry_delay(attempt, code))
                    if not b.translated and last_exc is not None:
                        code, message = classify_error(last_exc)
                        failures[i] = {
                            "block_id": b.id,
                            "page": b.page,
                            "code": code,
                            "message": message,
                            "attempts": 3,
                        }
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
                    final_manifest, report = snapshot()

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

            yield _sse({
                "type": "done",
                "translated": report.get("translated", 0),
                "new": done,
                "failed": report.get("failed", 0),
                "skipped": report.get("skipped", 0),
                "total": report.get("total", 0),
                "coverage": report.get("coverage", 0.0),
                "status": report.get("status", "partial"),
                "report": report,
            })
        except asyncio.CancelledError:
            # 客户端断开：把已完成的部分保存后静默退出
            if all_blocks:
                store.write_blocks(paper_id, all_blocks)
                snapshot(force_status="partial")
            raise
        except Exception as e:
            log.exception("流式翻译失败 %s", paper_id)
            code, message = classify_error(e)
            if all_blocks:
                try:
                    store.write_blocks(paper_id, all_blocks)
                    snapshot(
                        force_status="failed",
                        error={"code": code, "message": message},
                    )
                except Exception:
                    pass
            yield _sse({
                "type": "error",
                "code": code,
                "message": message,
                "detail": str(e)[:300],
            })
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


def _flush_job(paper_id: str, job: dict, all_blocks: list[dict]) -> None:
    store.write_blocks(paper_id, all_blocks)
    job["published_ids"] = list(job.get("translated_ids") or [])
    write_translation_job(DATA_DIR, job)


def _finish_job(
    paper_id: str,
    job: dict,
    all_blocks: list[dict],
    *,
    fatal: dict | None = None,
) -> dict:
    lang = str(job.get("lang") or "zh-Hans")
    manifest = build_manifest(all_blocks, paper_id, lang)
    started = int(job.get("started_at") or time.time())
    report = build_report(
        manifest,
        started_at=started,
        new_translated=int(job.get("done") or 0),
        failures=list(job.get("failures") or []),
        usage={"calls": int(job.get("done") or 0) + len(job.get("failures") or [])},
    )
    write_report(DATA_DIR, paper_id, lang, report)
    job["report"] = report
    job["failed"] = int(report.get("failed") or 0)
    job["skipped"] = int(report.get("skipped") or 0)
    # 引擎阶段进度只在任务运行期间有意义，收尾时清空，避免前端读到过期阶段。
    job["stage"] = ""
    job["stage_done"] = 0
    job["stage_total"] = 0

    if job.get("cancel_requested"):
        # 显式取消 / 切换语种优先；任务被 cancel 时携带的 interrupted
        # 不能把状态覆盖回 paused。
        job["status"] = "cancelled"
        if fatal is not None and fatal.get("code") not in {"interrupted", None}:
            job["error"] = fatal
    elif fatal is not None:
        code = str(fatal.get("code") or "unknown")
        job["error"] = fatal
        job["status"] = "failed" if code in FATAL_CODES else "paused"
    elif report.get("status") == "succeeded":
        job["status"] = "succeeded"
    elif report.get("status") == "failed":
        job["status"] = "failed"
    else:
        job["status"] = "partial"

    job["finished_at"] = int(time.time())
    job["resume_available"] = (
        job["status"] in {"paused", "failed", "cancelled"}
        and int(report.get("pending") or 0) > 0
    )
    # 引擎跑过的话，把它的成品 PDF 登记为译文 PDF（按最终 blocks 计算缓存键）。
    _mark_engine_pdf(paper_id, job)
    write_translation_job(DATA_DIR, job)
    return job


async def _try_engine_translation(
    paper_id: str,
    job: dict,
    cfg: LLMConfigIn,
    all_blocks: list[dict],
    targets: list[tuple[int, dict]],
    manifest: dict,
) -> bool:
    """用 BabelDOC 整篇翻译并映射回 Block。

    只处理「整篇」任务：引擎一次翻完整篇，按页任务（阅读页滚动时的增量翻译）
    仍走分块模型路径。返回 False 表示引擎不可用/运行失败/没有映射到任何块，
    调用方据此显式报错，不再回退到内部排版实现。
    """
    lang = str(job.get("lang") or "zh-Hans")
    if job.get("pages") or job.get("block_ids"):
        return False
    if not babeldoc_engine.available(lang):
        return False
    pdf = store.pdf_path(paper_id)
    if not pdf:
        return False

    base_done = int(job.get("done") or 0)
    progress_state = {"at": 0.0, "stage": ""}

    def on_progress(stage: str, stage_done: int, stage_total: int) -> None:
        # 引擎按自己的阶段推进：这里只上报它真实的阶段与阶段内计数，
        # 不再把比例换算成「已翻译块数」——两套计数没有一一对应关系。
        now = time.time()
        changed = stage != progress_state["stage"]
        if not changed and now - progress_state["at"] < 1.0:
            return
        progress_state["at"] = now
        progress_state["stage"] = stage
        job["stage"] = stage
        job["stage_done"] = int(stage_done)
        job["stage_total"] = int(stage_total)
        try:
            write_translation_job(DATA_DIR, job)
        except Exception:
            pass

    try:
        engine_dir = _engine_output_dir(paper_id, lang)
        # 只留单语成品：阅读页的「对照」是左原文 PDF + 右译文 PDF 两栏，
        # 不需要引擎把双语并排印在同一份 PDF 里。
        engine_pdf = engine_dir / "babeldoc.mono.pdf"
        # 引擎启动到第一条进度行之间有一段空白：先标成解析阶段，
        # 前端就不用在这段时间显示不准的块数。
        job["stage"] = "parse"
        job["stage_done"] = 0
        job["stage_total"] = 0
        try:
            write_translation_job(DATA_DIR, job)
        except Exception:
            pass
        il, engine_stats = await babeldoc_engine.run(
            pdf,
            {
                "api_key": cfg.api_key,
                "base_url": cfg.base_url.rstrip("/"),
                "model": cfg.model,
                "lang": lang,
            },
            on_progress=on_progress,
            should_cancel=lambda: bool(job.get("cancel_requested")),
            save_pdf=engine_pdf,
        )
    except babeldoc_engine.EngineCancelled:
        job["cancel_requested"] = True
        job["done"] = base_done
        return False
    except Exception as exc:  # noqa: BLE001 - 引擎任何异常都只降级，不影响主流程
        log.warning("BabelDOC 引擎失败：%s", exc)
        job["done"] = base_done
        return False

    eligible = set(manifest["eligible_ids"])
    mapped, stats = babeldoc_engine.map_translations(
        il, all_blocks, lang, eligible_ids=eligible
    )
    log.info("BabelDOC 映射结果：%s", stats)
    # engine_pdf_only：正文早就译完，这次只要引擎的成品 PDF，映射为空是正常的。
    pdf_only = bool(job.get("engine_pdf_only"))
    if not mapped and not pdf_only:
        job["done"] = base_done
        return False

    pending_pages: dict[int, int] = {}
    for index, block in targets:
        bid = str(block.get("id") or "")
        text = mapped.get(bid)
        if not text:
            continue
        all_blocks[index]["translated"] = text
        if bid not in (job.get("translated_ids") or []):
            job.setdefault("translated_ids", []).append(bid)
        page = int(block.get("page") or 0)
        pending_pages[page] = pending_pages.get(page, 0) + 1

    # 按页落盘：前端轮询能看到译文逐页出现，而不是最后一次性刷新
    for page in sorted(pending_pages):
        job["current_page"] = page
        job["done"] = len(job.get("translated_ids") or [])
        _flush_job(paper_id, job, all_blocks)
        await asyncio.sleep(0)

    job["done"] = len(job.get("translated_ids") or [])
    job["concurrency"] = 1
    _flush_job(paper_id, job, all_blocks)

    # 引擎成品 PDF 要等 blocks 全部落盘后再登记：缓存键按最终 blocks 计算，
    # 否则后面 LLM 补齐的块会让 PDF 被判定过期。
    saved_pdf = engine_stats.get("mono_pdf")
    if saved_pdf:
        job["_engine_pdf"] = saved_pdf
        job["_engine_pages"] = len(il.get("page") or [])
    return bool(pending_pages or job.get("_engine_pdf"))


async def _run_translation_job(
    paper_id: str,
    job_id: str,
    cfg: LLMConfigIn,
    auto_highlights: bool = False,
) -> None:
    """后台翻译任务：HTTP 断开不影响运行；每页落盘，重启后可续跑。"""
    job = _job_by_id(job_id)
    if job is None:
        return
    lang = str(job.get("lang") or "zh-Hans")
    target_name = _target_name(lang)
    first_start = not job.get("started_at")
    job["status"] = "running"
    job["started_at"] = int(job.get("started_at") or time.time())
    job["cancel_requested"] = False
    job["error"] = None
    job["resume_available"] = False
    write_translation_job(DATA_DIR, job)

    all_blocks: list[dict] = []
    fatal: dict | None = None
    try:
        from paperead.llm import set_backend

        set_backend(
            OpenAICompatBackend(
                LLMConfig(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url.rstrip("/"),
                    model=cfg.model,
                    timeout=cfg.timeout,
                )
            )
        )
        async with _lock_for(paper_id):
            if first_start and _reset_for_language(paper_id, lang):
                job["translated_ids"] = []
                job["published_ids"] = []
                job["done"] = 0
            all_blocks = store.read_blocks(paper_id) or []
            manifest = build_manifest(all_blocks, paper_id, lang)
            eligible = set(manifest["eligible_ids"])
            targets = [
                (i, b)
                for i, b in enumerate(all_blocks)
                if b.get("id") in eligible and not (b.get("translated") or "").strip()
            ]
            job["total"] = len(job.get("translated_ids") or []) + len(targets)
            job["skipped"] = manifest["skipped"]
            _flush_job(paper_id, job, all_blocks)
            if not targets:
                if job.get("engine_pdf_only"):
                    # 正文已有译文、只缺成品 PDF：跑一遍引擎补出成品。
                    if not await _try_engine_translation(
                        paper_id, job, cfg, all_blocks, [], manifest
                    ):
                        fatal = {
                            "code": "engine_failed",
                            "message": "全局翻译引擎没能生成译文 PDF，任务已暂停；可稍后重试。",
                        }
                _finish_job(paper_id, job, all_blocks, fatal=fatal)
                return

            # 全局翻译只走 BabelDOC 引擎：引擎不可用或运行失败都显式报错，
            # 不再用内部模型分块补译，避免两套全局翻译实现互相干扰。
            if not babeldoc_engine.available(lang):
                _finish_job(
                    paper_id,
                    job,
                    all_blocks,
                    fatal={"code": "engine_missing", "message": _ENGINE_MISSING},
                )
                return
            if not await _try_engine_translation(
                paper_id, job, cfg, all_blocks, targets, manifest
            ):
                if job.get("cancel_requested"):
                    _flush_job(paper_id, job, all_blocks)
                    _finish_job(paper_id, job, all_blocks)
                    return
                _finish_job(
                    paper_id,
                    job,
                    all_blocks,
                    fatal={
                        "code": "engine_failed",
                        "message": "全局翻译引擎未能完成翻译，任务已暂停；可稍后重试。",
                    },
                )
                return
            _flush_job(paper_id, job, all_blocks)
            _finish_job(paper_id, job, all_blocks, fatal=fatal)
            return

    except asyncio.CancelledError:
        if all_blocks:
            _flush_job(paper_id, job, all_blocks)
            _finish_job(
                paper_id,
                job,
                all_blocks,
                fatal={
                    "code": "interrupted",
                    "message": "任务被中断，进度已保存，可继续翻译。",
                },
            )
        raise
    except Exception as exc:
        code, message = classify_error(exc)
        log.exception("翻译任务失败 %s/%s", paper_id, job_id)
        if all_blocks:
            _flush_job(paper_id, job, all_blocks)
            _finish_job(
                paper_id,
                job,
                all_blocks,
                fatal={"code": code, "message": message},
            )
        else:
            job["status"] = "failed"
            job["error"] = {"code": code, "message": message}
            job["finished_at"] = int(time.time())
            job["resume_available"] = True
            write_translation_job(DATA_DIR, job)
    finally:
        try:
            write_translation_job(DATA_DIR, job)
        except Exception:
            pass


async def _parse_task(paper_id: str, vision_config: LLMConfig | None = None) -> None:
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
        if not parsed.blocks:
            # 无文本层/扫描版：不能标 ready，否则用户进入阅读页只会看到空白，
            # 翻译入口也会永远「0 块待译」且没有原因。
            store.write_blocks(paper_id, [])
            store.update(
                paper_id,
                status="failed",
                blocks=0,
                pages=parsed.page_count,
                error="没有解析出正文块：可能是扫描版或 PDF 没有文本层",
            )
            log.warning("解析为空（疑似扫描版）：%s", paper_id)
            return
        # 配了识图模型就把显示公式转成 LaTeX；阅读页优先用 KaTeX 矢量渲染，
        # 识别失败或没配置时自动回落到裁图，不影响阅读。
        cfg = vision_config or _vision_config
        if cfg is not None:
            try:
                stats = await asyncio.wait_for(
                    recognize_equations(
                        parsed.blocks,
                        concurrency=6,
                        backend=OpenAICompatBackend(cfg),
                    ),
                    timeout=FORMULA_TIMEOUT,
                )
                log.info("公式识别 %s：%s", paper_id, stats)
                # 行内公式没有独立裁图资产，按 bbox 现场裁图识别；
                # 结果可编译才写入 latex，否则渲染层回退裁图。
                inline_stats = await asyncio.wait_for(
                    recognize_inline_math(
                        pdf,
                        parsed.blocks,
                        concurrency=6,
                        backend=OpenAICompatBackend(cfg),
                    ),
                    timeout=FORMULA_TIMEOUT * 2,
                )
                log.info("行内公式识别 %s：%s", paper_id, inline_stats)
            except asyncio.TimeoutError:
                log.warning("公式识别超时（%ss），剩余公式保留裁图", FORMULA_TIMEOUT)
            except Exception:
                log.exception("公式识别失败 %s", paper_id)
        else:
            log.info("未配置识图模型，公式保留裁图：%s", paper_id)
        if cfg is not None:
            try:
                from paperead.vision_text import enrich_paragraphs

                stats = await asyncio.wait_for(
                    enrich_paragraphs(pdf, parsed.blocks, backend=OpenAICompatBackend(cfg)),
                    timeout=FORMULA_TIMEOUT,
                )
                log.info("正文识图 %s：%s", paper_id, stats)
            except asyncio.TimeoutError:
                log.warning("正文识图超时（%ss），未完成的段落保留原结果", FORMULA_TIMEOUT)
            except Exception:
                log.exception("正文识图失败 %s，保留原结果", paper_id)
        blocks: list[dict] = []
        for b in parsed.blocks:
            d = b.to_dict()
            if d.get("image_path"):
                try:
                    rel = Path(d["image_path"]).resolve().relative_to(assets.resolve())
                    d["image_path"] = f"/api/papers/{paper_id}/asset/{rel.as_posix()}"
                except Exception:
                    d["image_path"] = None
            for item in d.get("inline_math") or []:
                if not item.get("image_path"):
                    continue
                try:
                    rel = Path(item["image_path"]).resolve().relative_to(assets.resolve())
                    item["image_path"] = (
                        f"/api/papers/{paper_id}/asset/{rel.as_posix()}"
                    )
                except Exception:
                    item["image_path"] = None
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
    """探测本机 Zotero：优先直接读本机数据库（无需 Key）。

    拉取界面不要求用户配置 Web API Key，也不应等本地 HTTP 端口超时；
    本机数据库可读就直接返回，本地 API 只在数据库不可读时作为补充。
    """
    data_dir = _zotero_data_dir()
    local = await asyncio.to_thread(zotero_local.status)
    local_payload = {
        "ok": True,
        "status": 200,
        "version": "local-db",
        "source": "local-db",
        "data_dir": local.get("data_dir") or (str(data_dir) if data_dir.exists() else ""),
        "total": int(local.get("total") or 0),
        "error": "",
    }
    if local.get("ok"):
        return local_payload
    try:
        async with _zotero_client(timeout=3) as c:
            r = await c.get(f"{ZOTERO_BASE}/items", params={"limit": 1})
    except Exception as e:
        return {
            "ok": False,
            "error": f"连不上本机 Zotero（{e}）。{_ZOTERO_HINT}",
            "local_error": local.get("error", ""),
        }
    if r.status_code == 403:
        return {
            "ok": False,
            "status": 403,
            "error": _ZOTERO_HINT,
            "local_error": local.get("error", ""),
        }
    return {
        "ok": r.status_code == 200,
        "status": r.status_code,
        "version": r.headers.get("Zotero-API-Version") or "",
        "data_dir": str(data_dir) if data_dir.exists() else "",
        "source": "local-api",
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


def _zotero_mark_existing(items: list[dict]) -> list[dict]:
    """给本地库回退的条目补上「已在库」和缩略图地址。"""
    existing = store.list()
    by_source = {
        str(row.get("source_id")): row for row in existing if row.get("source_id")
    }
    by_title: dict[str, str] = {}
    for row in existing:
        key = _normalize_title(row.get("title"))
        if key:
            by_title.setdefault(key, row["id"])
    for item in items:
        key = str(item.get("key") or "")
        title = str(item.get("title") or "").strip()
        duplicate = by_source.get(f"zotero:{key}") if key else None
        if not duplicate and title:
            duplicate = by_title.get(_normalize_title(title))
        item["imported"] = bool(duplicate)
        if item.get("pdf") and not item.get("thumb_url"):
            item["thumb_url"] = f"/api/zotero/thumb?key={quote(key)}"
    return items


@app.get("/api/zotero/items")
async def zotero_items(
    collection: str = Query("", max_length=40),
    q: str = Query("", max_length=200),
    limit: int = Query(50, ge=1, le=100),
    start: int = Query(0, ge=0),
) -> dict:
    """分页列出 Zotero 条目：优先本机数据库（免 Key、立即返回），再回退本地 API。"""
    local_status = await asyncio.to_thread(zotero_local.status)
    if local_status.get("ok"):
        local_items, total = await asyncio.to_thread(
            zotero_local.list_items,
            q=q,
            limit=limit,
            start=start,
            collection=collection.strip(),
        )
        items = _zotero_mark_existing(local_items)
        return {
            "items": items,
            "total": total,
            "start": start,
            "limit": limit,
            "has_more": start + len(items) < total,
        }

    try:
        async with _zotero_client() as c:
            pairs, total = await _zotero_candidates(
                c,
                collection=collection.strip(),
                q=q,
                limit=limit,
                start=start,
            )
    except ZoteroError as api_error:
        raise HTTPException(status_code=502, detail=str(api_error)) from api_error

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
                "thumb_url": (
                    f"/api/zotero/thumb?key={quote(str(key or ''))}"
                    if pdf
                    else None
                ),
            }
        )
    return {
        "items": items,
        "total": total,
        "start": start,
        "limit": limit,
        "has_more": start + len(items) < total,
    }


@app.get("/api/zotero/thumb")
async def zotero_thumb(key: str = Query(..., max_length=40)) -> FileResponse:
    """按需把 Zotero 条目的 PDF 首页渲染成缩略图，缓存到数据目录。"""
    safe = "".join(ch for ch in key if ch.isalnum() or ch in "-_") or "item"
    cache = DATA_DIR / "thumb-cache" / f"zotero-{safe}.png"
    if cache.is_file():
        return FileResponse(cache, media_type="image/png")
    try:
        async with _zotero_client() as client:
            pairs, _total = await _zotero_candidates(
                client, keys=[key], limit=1
            )
            if not pairs or not pairs[0][1]:
                raise HTTPException(status_code=404, detail="Zotero 条目没有 PDF")
            blob = await _zotero_file_bytes(client, pairs[0][1])
    except Exception as api_error:
        path = await asyncio.to_thread(zotero_local.pdf_path, key)
        if path is None:
            raise HTTPException(
                status_code=404, detail="Zotero 条目没有可读取的 PDF"
            ) from api_error
        try:
            blob = await asyncio.to_thread(path.read_bytes)
        except OSError as read_error:
            raise HTTPException(
                status_code=404, detail="Zotero PDF 文件不可读"
            ) from read_error
    tmp = DATA_DIR / "thumb-cache" / f"zotero-{safe}.pdf"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(blob)
    try:
        await asyncio.to_thread(render_thumbnail, tmp, cache)
    finally:
        tmp.unlink(missing_ok=True)
    return FileResponse(cache, media_type="image/png")


@app.get("/api/zotero/pdf")
async def zotero_pdf(key: str = Query(..., max_length=40)) -> Response:
    """按需读取 Zotero 条目的 PDF 原文，供拉取前预览；免 Key 优先读本机库。"""
    path = await asyncio.to_thread(zotero_local.pdf_path, key)
    if path is not None:
        try:
            return FileResponse(
                path,
                media_type="application/pdf",
                headers={"Content-Disposition": "inline"},
            )
        except OSError as error:
            raise HTTPException(status_code=404, detail="Zotero PDF 文件不可读") from error
    try:
        async with _zotero_client(timeout=30) as client:
            pairs, _total = await _zotero_candidates(client, keys=[key], limit=1)
            if not pairs or not pairs[0][1]:
                raise HTTPException(status_code=404, detail="Zotero 条目没有 PDF")
            blob = await _zotero_file_bytes(client, pairs[0][1])
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=404, detail="Zotero 条目没有可读取的 PDF"
        ) from error
    return Response(content=blob, media_type="application/pdf")


class ZoteroImportReq(BaseModel):
    # keys 为空时按 collection/q/limit 批量挑带 PDF 的条目（兼容旧的一键导入）
    keys: list[str] = Field(default_factory=list, max_length=100)
    collection: str = ""
    q: str = ""
    limit: int = Field(20, ge=1, le=100)


async def _zotero_import_local(req: ZoteroImportReq) -> dict:
    """无本地 API / 无 Web Key 时，直接从本机 Zotero 数据库导入。"""
    items, total = await asyncio.to_thread(
        zotero_local.list_items,
        q=req.q,
        limit=req.limit,
        collection=req.collection.strip(),
        keys=req.keys or None,
    )
    imported, skipped, errors, papers = 0, 0, [], []
    for item in items:
        key = str(item.get("key") or "")
        title = str(item.get("title") or "(无标题)")
        if not item.get("pdf"):
            skipped += 1
            continue
        if key and store.find_by_source(f"zotero:{key}"):
            skipped += 1
            continue
        try:
            path = await asyncio.to_thread(zotero_local.pdf_path, key)
            if path is None:
                skipped += 1
                continue
            blob = await asyncio.to_thread(path.read_bytes)
            meta = {
                "source": "zotero",
                "source_id": f"zotero:{key}",
                "title": title,
                "authors": str(item.get("authors") or ""),
                "year": str(item.get("year") or ""),
                "doi": str(item.get("doi") or ""),
                "url": str(item.get("url") or ""),
                "venue": str(item.get("publication") or ""),
            }
            row = store.add(_zotero_name(item), blob, meta)
            asyncio.create_task(_parse_task(row["id"]))
            papers.append(_public(row))
            imported += 1
        except Exception as error:
            errors.append(f"{title}: {error}"[:160])
    return {
        "imported": imported,
        "skipped": skipped,
        "scanned": total,
        "errors": errors[:10],
        "papers": papers,
    }


@app.post("/api/zotero/import")
async def zotero_import(req: ZoteroImportReq) -> dict:
    """把选中的 Zotero 条目（带 PDF 附件）导入文献库，并后台解析。"""
    imported, skipped, errors, papers = 0, 0, [], []
    # 本机 Zotero 可直接读取时优先走数据库，不依赖 Key / 本地 API
    local_status = await asyncio.to_thread(zotero_local.status)
    if local_status.get("ok"):
        local_items, _total = await asyncio.to_thread(
            zotero_local.list_items,
            q=req.q,
            limit=req.limit,
            collection=req.collection.strip(),
            keys=req.keys or None,
        )
        if local_items:
            return await _zotero_import_local(req)
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
    except ZoteroError:
        return await _zotero_import_local(req)

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
    # 本机 Zotero 有可导入条目时直接走数据库，不依赖 Key、也不等本地 API 超时
    local_status = await asyncio.to_thread(zotero_local.status)
    if local_status.get("ok"):
        local_items, _total = await asyncio.to_thread(
            zotero_local.list_items,
            limit=min(req.limit, 5000),
            collection=req.collection.strip(),
        )
        if local_items:
            return await _zotero_import_local(
                ZoteroImportReq(
                    collection=req.collection,
                    limit=min(req.limit, 5000),
                )
            )
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
    except ZoteroError:
        return await _zotero_import_local(
            ZoteroImportReq(collection=req.collection, limit=req.limit)
        )

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


# ── 插件阅读接口（/api/ext/v1/reading/*） ────────────────────────────────
#
# 扩展/用户脚本不持有模型配置：这里统一读取「设置 → 模型服务」里选中的
# answer 模型，再复用阅读页已有的翻译与问答实现，避免把 API key 带到页面。


def _require_ext_plugin(scope: str, request: Request) -> dict:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少插件 token")
    token = authorization.removeprefix("Bearer ").strip()
    plugin = plugin_registry.verify(token, request.headers.get("origin"))
    if plugin is None:
        raise HTTPException(status_code=401, detail="插件 token 无效或已撤销")
    if scope not in plugin.scopes:
        raise HTTPException(status_code=403, detail=f"插件缺少 {scope} 权限")
    return {"id": plugin.id, "name": plugin.name}


def _saved_answer_model() -> LLMConfigIn:
    """读取设置页选中的 answer 模型；没有可用配置时给出可操作提示。"""
    payload = _read_model_settings()
    settings = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings, dict):
        raise HTTPException(
            status_code=400,
            detail="未配置模型，请先在 Paperead 设置 → 模型服务 中配置",
        )
    models = [
        item
        for item in settings.get("models") or []
        if isinstance(item, dict) and item.get("kind") == "answer"
    ]
    active = settings.get("active") if isinstance(settings.get("active"), dict) else {}
    active_id = str(active.get("answer") or "")
    chosen = next(
        (item for item in models if str(item.get("id") or "") == active_id),
        models[0] if models else None,
    )
    if not isinstance(chosen, dict):
        raise HTTPException(
            status_code=400,
            detail="未配置可用模型，请先在 Paperead 设置 → 模型服务 中配置",
        )
    base_url = str(chosen.get("baseUrl") or "").strip().rstrip("/")
    api_key = str(chosen.get("apiKey") or "").strip()
    model = str(chosen.get("model") or "").strip()
    if not (base_url and api_key and model):
        raise HTTPException(
            status_code=400,
            detail="已选模型缺少地址、密钥或模型名，请先在设置中补全",
        )
    return LLMConfigIn(base_url=base_url, api_key=api_key, model=model)


class ExtTranslateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=6000)
    target_lang: str = "zh-Hans"


class ExtAskIn(BaseModel):
    paper_id: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1, max_length=2000)
    web: bool = False


@app.get("/api/ext/v1/reading/status")
def ext_reading_status(request: Request) -> dict:
    _require_ext_plugin("reading:meta", request)
    try:
        config = _saved_answer_model()
    except HTTPException:
        return {"model_ready": False, "model_label": ""}
    return {"model_ready": True, "model_label": config.model}


@app.post("/api/ext/v1/reading/translate")
async def ext_reading_translate(request: Request, req: ExtTranslateIn) -> dict:
    _require_ext_plugin("reading:translate", request)
    config = _saved_answer_model()
    return await translate_text_api(
        TranslateTextReq(text=req.text, target_lang=req.target_lang, config=config)
    )


@app.post("/api/ext/v1/reading/ask")
async def ext_reading_ask(request: Request, req: ExtAskIn) -> dict:
    _require_ext_plugin("reading:ask", request)
    if not store.get(req.paper_id):
        raise HTTPException(status_code=404, detail="论文不在本地库")
    config = _saved_answer_model()
    return await ask_paper(
        req.paper_id,
        AskReq(question=req.question, web=req.web, config=config),
    )


@app.post("/api/ext/v1/reading/ask/stream")
async def ext_reading_ask_stream(request: Request, req: ExtAskIn) -> StreamingResponse:
    _require_ext_plugin("reading:ask", request)
    if not store.get(req.paper_id):
        raise HTTPException(status_code=404, detail="论文不在本地库")
    config = _saved_answer_model()
    return await ask_stream(
        req.paper_id,
        AskReq(question=req.question, web=req.web, config=config),
    )


class ExtPaperIn(BaseModel):
    paper_id: str = Field(..., min_length=1, max_length=64)


class ExtHighlightsIn(BaseModel):
    paper_id: str = Field(..., min_length=1, max_length=64)
    pages: list[int] = Field(default_factory=list, max_length=12)
    force: bool = False


class ExtAnnotationIn(BaseModel):
    paper_id: str = Field(..., min_length=1, max_length=64)
    quote: str = Field(..., min_length=1, max_length=4000)
    note: str = Field("", max_length=5000)
    block_id: str = Field("", max_length=24)
    page: int = Field(0, ge=0, le=100000)


def _ext_translated_pages(paper_id: str) -> int:
    """已有译文的页数（blocks 里 translatable 且 translated 非空）。"""
    blocks = store.read_blocks(paper_id) or []
    return len(
        {
            int(row.get("page") or 0)
            for row in blocks
            if row.get("translatable") and (row.get("translated") or "").strip()
        }
    )


@app.get("/api/ext/v1/reading/guide")
async def ext_reading_guide(
    request: Request,
    paper_id: str = Query(..., min_length=1, max_length=64),
) -> dict:
    _require_ext_plugin("reading:meta", request)
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不在本地库")
    path = store.papers_dir / f"{paper_id}.guide.json"
    if not path.exists():
        return {"status": "missing", "guide": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"status": "missing", "guide": None}
    if data.get("version") != GUIDE_VERSION:
        return {"status": "stale", "guide": None}
    return {"status": data.get("status") or "ready", "guide": data}


@app.post("/api/ext/v1/reading/guide")
async def ext_make_reading_guide(request: Request, req: ExtPaperIn) -> dict:
    _require_ext_plugin("reading:ask", request)
    if not store.get(req.paper_id):
        raise HTTPException(status_code=404, detail="论文不在本地库")
    config = _saved_answer_model()
    try:
        guide = await make_guide(req.paper_id, GuideReq(config=config))
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"导读生成失败：{error}") from error
    return {"status": guide.get("status") or "ready", "guide": guide}


@app.get("/api/ext/v1/reading/highlights")
async def ext_reading_highlights(
    request: Request,
    paper_id: str = Query(..., min_length=1, max_length=64),
) -> dict:
    _require_ext_plugin("reading:meta", request)
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不在本地库")
    return {
        "highlights": store.list_highlights(paper_id),
        "pages": store.highlight_pages(paper_id),
        "categories": HIGHLIGHT_CATEGORIES,
        "translated_pages": _ext_translated_pages(paper_id),
    }


@app.post("/api/ext/v1/reading/highlights")
async def ext_make_reading_highlights(request: Request, req: ExtHighlightsIn) -> dict:
    _require_ext_plugin("reading:ask", request)
    if not store.get(req.paper_id):
        raise HTTPException(status_code=404, detail="论文不在本地库")
    config = _saved_answer_model()
    translated_pages = _ext_translated_pages(req.paper_id)
    try:
        data = await make_reading_highlights(
            req.paper_id,
            HighlightsReq(config=config, pages=req.pages, force=req.force),
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"阅读重点生成失败：{error}") from error
    return {
        **data,
        "translated_pages": translated_pages,
        "needs_translation": translated_pages == 0,
    }


@app.get("/api/ext/v1/reading/annotations")
async def ext_reading_annotations(
    request: Request,
    paper_id: str = Query(..., min_length=1, max_length=64),
) -> dict:
    _require_ext_plugin("reading:meta", request)
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不在本地库")
    return {"annotations": store.list_annotations(paper_id)}


@app.post("/api/ext/v1/reading/annotations", status_code=201)
async def ext_add_reading_annotation(request: Request, req: ExtAnnotationIn) -> dict:
    _require_ext_plugin("reading:annotate", request)
    if not store.get(req.paper_id):
        raise HTTPException(status_code=404, detail="论文不在本地库")
    quote = req.quote.strip()
    if not quote:
        raise HTTPException(status_code=400, detail="批注缺少选中文字")
    block_id = req.block_id.strip()
    start = end = 0
    if block_id:
        block = next(
            (row for row in (store.read_blocks(req.paper_id) or []) if row.get("id") == block_id),
            None,
        )
        text = (block or {}).get("text") or ""
        pos = text.find(quote)
        if pos >= 0:
            start, end = pos, pos + len(quote)
    return store.add_annotation(
        req.paper_id,
        {
            "block_id": block_id,
            "source": "en",
            "start": start,
            "end": end,
            "quote": req.quote,
            "color": "yellow",
            "kind": "comment" if req.note.strip() else "highlight",
            "note": req.note,
            "pair": None,
            "page": req.page or None,
        },
    )


# 整份设置备份：localStorage 跟着端口走，数据目录与端口无关，换端口也能恢复。
class StorageRelocateIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
            str(Path(right).resolve())
        )
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


@app.get("/api/storage")
def get_storage() -> dict:
    """当前文献库目录与自定义状态；自定义路径失效时返回回退信息。"""
    configured = load_data_location()
    configured_path = str(configured) if configured is not None else ""
    differs = bool(configured is not None and not _same_path(configured, DATA_DIR))
    try:
        configured_exists = bool(configured is not None and configured.exists())
    except OSError:
        configured_exists = False
    # 迁移完成后新路径已写入配置、但当前进程还开着旧目录：这是“等待重启”，
    # 不是“自定义路径失效”，界面不能提示成不可用。
    pending = differs and configured_exists
    return {
        "path": str(DATA_DIR),
        "configured": configured is not None,
        "configured_path": configured_path,
        "pending": pending,
        "fallback": differs and not configured_exists,
        "writable": os.access(DATA_DIR, os.W_OK),
    }


@app.post("/api/storage/relocate", status_code=202)
def relocate_storage(req: StorageRelocateIn) -> dict:
    """启动后台迁移，前端轮询进度；复制成功后写入新路径并等待重启。"""
    try:
        job = storage_migration.start_relocate(
            DATA_DIR,
            Path(req.path),
            on_success=lambda result: save_data_location(
                Path(result["path"]), previous=DATA_DIR
            ),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"job_id": job.id, "status": job.snapshot()}


@app.get("/api/storage/relocate/active")
def relocate_storage_active() -> dict:
    """当前是否有正在进行的迁移；前端切换界面后重新挂载时用它恢复进度。"""
    return {"job": storage_migration.active_job()}


@app.get("/api/storage/relocate/{job_id}")
def relocate_storage_status(job_id: str) -> dict:
    """迁移进度：phase 为 done 时结果在 result 里，failed 时 error 是原因。"""
    job = storage_migration.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="迁移任务不存在或已过期")
    return job


APP_SETTINGS_PATH = DATA_DIR / "app-settings.json"
APP_SETTINGS_VALUE_LIMIT = 256 * 1024
APP_SETTINGS_KEY_LIMIT = 200


@app.get("/api/settings/app")
def get_app_settings() -> dict:
    try:
        data = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


@app.put("/api/settings/app")
def put_app_settings(payload: dict) -> dict:
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings 必须是对象")
    clean: dict[str, str] = {}
    for key, value in settings.items():
        if len(clean) >= APP_SETTINGS_KEY_LIMIT:
            break
        if not isinstance(key, str) or not key.startswith("paperead."):
            continue
        if not isinstance(value, str) or len(value) > APP_SETTINGS_VALUE_LIMIT:
            continue
        clean[key] = value
    data = {
        "settings": clean,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    APP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = APP_SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, APP_SETTINGS_PATH)
    return {"ok": True, "keys": len(clean), "updated_at": data["updated_at"]}


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


def _schedule_scholar_daily_prefetch() -> None:
    """服务一启动就在后台确认当天推荐，用户打开检索页时直接命中。"""
    if env("PAPEREAD_SCHOLAR_PREFETCH", "1") == "0":
        return
    asyncio.create_task(_warm_scholar_daily())


async def _warm_scholar_daily() -> None:
    try:
        info = await prefetch_scholar_daily()
        log.info(
            "每日推荐预热完成：%s（%d 篇，%s）",
            info.get("date"),
            info.get("pool_size") or 0,
            "命中当天缓存" if info.get("cached") else "已拉取",
        )
    except Exception as exc:
        log.warning("每日推荐预热失败：%s", exc)


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
    if env("PAPEREAD_NOTES_AUTOSYNC", "1") != "0":
        notes_mirror.auto_sync()
    uvicorn.run(app, host=host, port=PORT, log_config=None, access_log=False)


if __name__ == "__main__":
    main()

