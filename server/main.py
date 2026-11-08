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
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from paperead import __version__
from paperead.env import env
from paperead.formula import recognize_latex
from paperead.layout import Block, parse_pdf
from paperead.llm import LLMConfig, OpenAICompatBackend
from paperead.tutor import build_guide
from paperead.tutor import memory as memory_engine
from paperead.tutor import qa as qa_engine
from paperead.tutor import web as web_search
from paperead.tutor.citations import find as find_citations, resolve as resolve_citations
from paperead.tutor.lineage import build_lineage
from paperead.tutor.provenance import enrich as enrich_provenance
from paperead.tutor.references import Reference, parse as parse_references
from paperead.translate import (
    build_glossary,
    quality_ok,
    translate_block,
    translate_paper,
)
from server.store import LibraryStore
from server.scholar_api import build_router as build_scholar_router
from server.runtime import (
    allowed_origins,
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

store = LibraryStore(DATA_DIR)

app = FastAPI(title="Paperead", version=__version__)


@app.middleware("http")
async def guard(request: Request, call_next):
    """本地 API 防护：cookie/header token + Origin 校验。

    桌面壳把随机 token 作为 HttpOnly cookie 在首页下发，前端无需改代码；
    壳自己用 X-Paperead-Token 调 health。直接 python -m server.main 时
    TOKEN 为空，鉴权关闭，保持原有开发体验。
    """
    path = request.url.path
    if path.startswith("/api"):
        # 旧版壳/脚本可能还在发 x-paper-reflow-token，继续接受。
        supplied = (
            request.cookies.get("pr_token")
            or request.headers.get("x-paperead-token")
            or request.headers.get("x-paper-reflow-token")
        )
        if TOKEN and supplied != TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in ORIGINS:
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)

    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        # 前端更新后 index.html 引用的是新 hash；禁止缓存 HTML，
        # 避免用户刷新后仍加载旧的 assets/*.js。
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        if TOKEN and "pr_token" not in request.cookies:
            response.set_cookie(
                "pr_token", TOKEN, httponly=True, samesite="strict", path="/"
            )
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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


@app.get("/api/research/agent")
async def research_agent() -> dict:
    """A0 spike handshake: the desktop shell writes agent.json after the Node
    agent is healthy. A2 replaces this direct endpoint with a Python proxy."""
    path = DATA_DIR / "agent.json"
    if not path.exists():
        raise HTTPException(status_code=503, detail="研究 agent 尚未启动")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"agent.json 无效：{e}") from e
    if not isinstance(data, dict) or not data.get("port"):
        raise HTTPException(status_code=503, detail="agent.json 缺少 port")
    return data


def _agent_target() -> tuple[str, str]:
    path = DATA_DIR / "agent.json"
    if not path.exists():
        raise HTTPException(status_code=503, detail="研究 agent 尚未启动")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"agent.json 无效：{e}") from e
    if not isinstance(data, dict) or not data.get("port"):
        raise HTTPException(status_code=503, detail="agent.json 缺少 port")
    return f"http://127.0.0.1:{int(data['port'])}", str(data.get("token") or "")


@app.api_route(
    "/api/research/{path:path}",
    methods=["GET", "POST", "DELETE", "PATCH"],
)
async def research_proxy(path: str, request: Request):
    """Thin SSE/JSON proxy from the product API to the local Node agent.

    The frontend never sees the agent port or token. A2 keeps this route and
    replaces the agent.json lookup with the project/session store.
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


@app.get("/api/papers")
async def list_papers() -> dict:
    return {"papers": [_public(r) for r in store.list()], "mock": MOCK}


@app.post("/api/papers/upload")
async def upload_papers(files: list[UploadFile] = File(...)) -> dict:
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
        row = store.add(name, data)
        added.append(_public(row))
        # 上传后立刻后台解析（布局 + 图表 + 公式裁图），不阻塞上传响应
        asyncio.create_task(_parse_task(row["id"]))
    return {"added": added, "failed": failed}


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
        "blocks": blocks,
    }


@app.get("/api/papers/{paper_id}/guide")
async def get_guide(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    path = store.papers_dir / f"{paper_id}.guide.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="导读还没生成")
    return json.loads(path.read_text(encoding="utf-8-sig"))


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
        return json.loads(path.read_text(encoding="utf-8-sig"))

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


@app.get("/api/papers/{paper_id}/lineage")
async def get_lineage(paper_id: str) -> dict:
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    path = store.papers_dir / f"{paper_id}.lineage.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="脉络还没生成")
    return json.loads(path.read_text(encoding="utf-8-sig"))


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
        return json.loads(path.read_text(encoding="utf-8-sig"))
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
        data = await build_lineage(objs)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data
    finally:
        _lineaging.discard(paper_id)


class AskReq(BaseModel):
    question: str
    config: LLMConfigIn | None = None
    web: bool = True
    client_id: str | None = None


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
        blocks = await _translate_pages(paper_id, req.config, pages)
        return {"ok": True, "status": "done", "pages": pages, "blocks": blocks}
    except Exception as e:
        log.exception("翻译失败 %s", paper_id)
        raise HTTPException(status_code=502, detail=f"翻译失败：{e}") from e
    finally:
        _translating.discard(key)


async def _translate_pages(
    paper_id: str, cfg: LLMConfigIn, pages: list[int] | None
) -> list[dict]:
    """翻译指定页的块，返回这些块的最新数据。"""
    async with _lock_for(paper_id):
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
            glossary = await build_glossary(everything)

        await translate_paper(objs, glossary, concurrency=8)

        for (idx, _), obj in zip(targets, objs):
            all_blocks[idx] = obj.to_dict()

        store.write_blocks(paper_id, all_blocks)
        done = sum(1 for b in all_blocks if b.get("translated"))
        store.update(paper_id, translated=done, glossary=glossary)
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
        try:
            async with _lock_for(paper_id):
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
                    glossary = await build_glossary(everything)
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
                                )
                                if quality_ok(b.text, tex) or attempt == 1:
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
                    store.update(paper_id, translated=sum(1 for b in all_blocks if b.get("translated")))

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
    assets = store.papers_dir / f"{paper_id}_assets"
    try:
        parsed = await asyncio.to_thread(parse_pdf, pdf, paper_id, assets)
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
    except Exception as e:
        log.exception("解析失败 %s", paper_id)
        store.update(paper_id, status="failed", error=str(e)[:300])
    finally:
        _parsing.discard(paper_id)


# ── Zotero（实验性） ────────────────────────────────────────────────────

ZOTERO_BASE = env("PAPEREAD_ZOTERO", "http://127.0.0.1:23119/api/users/0")


@app.get("/api/zotero/probe")
async def zotero_probe() -> dict:
    """探测本机 Zotero 7 的本地 API 是否开启。"""
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
            r = await c.get(f"{ZOTERO_BASE}/items", params={"limit": 1})
        return {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ZoteroImportReq(BaseModel):
    limit: int = 20


@app.post("/api/zotero/import")
async def zotero_import(req: ZoteroImportReq) -> dict:
    """从 Zotero 本地库里挑带 PDF 附件的条目导入。

    Zotero 7 需在「设置 → 高级」勾选允许其他应用访问本地 API，否则这里会连不上。
    """
    imported, skipped, errors = 0, 0, []
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
            r = await c.get(
                f"{ZOTERO_BASE}/items",
                params={"limit": req.limit, "itemType": "-attachment || note"},
            )
            r.raise_for_status()
            items = r.json()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"连不上 Zotero 本地 API（{e}）。请在 Zotero 设置 → 高级里允许其他应用访问，"
            "或直接把 PDF 拖进来。",
        ) from e

    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        for item in items:
            data = item.get("data") or {}
            if data.get("itemType") in ("attachment", "note"):
                continue
            key = item.get("key") or data.get("key")
            if not key:
                continue
            try:
                kids = (
                    await c.get(f"{ZOTERO_BASE}/items/{key}/children")
                ).json()
                pdf_att = next(
                    (
                        k for k in kids
                        if (k.get("data") or {}).get("contentType") == "application/pdf"
                    ),
                    None,
                )
                if not pdf_att:
                    skipped += 1
                    continue
                att_key = pdf_att.get("key")
                resp = await c.get(f"{ZOTERO_BASE}/items/{att_key}/file")
                resp.raise_for_status()
                store.add(_zotero_name(data), resp.content)
                imported += 1
            except Exception as e:
                errors.append(f"{data.get('title', key)}: {e}"[:160])

    return {"imported": imported, "skipped": skipped, "errors": errors[:5]}


def _zotero_name(data: dict) -> str:
    title = (data.get("title") or "zotero-paper")[:80]
    safe = "".join(ch for ch in title if ch not in '\\/:*?"<>|').strip() or "zotero"
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


if WEB_DIST.exists():
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
    log.info(
        "Paperead %s 启动：http://127.0.0.1:%d（数据：%s）",
        __version__,
        PORT,
        DATA_DIR,
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_config=None, access_log=False)


if __name__ == "__main__":
    main()

