"""paper-reflow 本地服务。

为什么需要它：国内厂商的 API 基本都不允许浏览器直连（CORS），
所以「前端填 Key、直接调模型」这条路走不通，必须有一个本地代理。
另外密钥只在浏览器 localStorage 里，随请求以 body 传给本服务，服务端不落盘。

启动：
    pip install -e ".[server]"
    python -m server.main          # 或 uvicorn server.main:app --port 8000

离线联调（不消耗额度、不需要 Key）：
    PAPER_REFLOW_MOCK=1 python -m server.main
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from paper_reflow.formula import recognize_latex
from paper_reflow.layout import Block, parse_pdf
from paper_reflow.llm import LLMConfig, OpenAICompatBackend
from paper_reflow.tutor import build_guide
from paper_reflow.tutor import qa as qa_engine
from paper_reflow.tutor import web as web_search
from paper_reflow.tutor.citations import find as find_citations, resolve as resolve_citations
from paper_reflow.tutor.lineage import build_lineage
from paper_reflow.tutor.provenance import enrich as enrich_provenance
from paper_reflow.tutor.references import Reference, parse as parse_references
from paper_reflow.translate import (
    build_glossary,
    quality_ok,
    translate_block,
    translate_paper,
)
from server.store import LibraryStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paper-reflow.server")

ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = ROOT / "web" / "dist"
DATA_DIR = Path(os.getenv("PAPER_REFLOW_DATA", str(ROOT / "data")))
MOCK = os.getenv("PAPER_REFLOW_MOCK") == "1"

store = LibraryStore(DATA_DIR)

app = FastAPI(title="paper-reflow", version="0.1.0")

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


def _decode(image_base64: str) -> bytes:
    if "," in image_base64[:80]:  # 容忍 data:image/png;base64, 前缀
        image_base64 = image_base64.split(",", 1)[1]
    try:
        return base64.b64decode(image_base64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"图片解码失败：{e}") from e


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "mock": MOCK, "web": WEB_DIST.exists()}


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
            [{"role": "user", "content": "回复两个字：正常"}],
            model=req.config.model,
            temperature=0.0,
            max_tokens=16,
        )
    except Exception as e:
        log.warning("连通性测试失败：%s", e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"ok": True, "ms": int((time.time() - started) * 1000), "reply": reply[:40]}


# ── 文献库 ──────────────────────────────────────────────────────────────

_parsing: set[str] = set()
_translating: set[str] = set()
_locks: dict[str, asyncio.Lock] = {}


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


@app.delete("/api/papers/{paper_id}")
async def delete_paper(paper_id: str) -> dict:
    if not store.remove(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
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
    return json.loads(path.read_text(encoding="utf-8"))


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
        return json.loads(path.read_text(encoding="utf-8"))

    if paper_id in _guiding:
        raise HTTPException(status_code=429, detail="导读正在生成中")

    if req and req.config:
        from paper_reflow.llm import set_backend

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
    elif not os.getenv("PAPER_REFLOW_API_KEY"):
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
    return json.loads(path.read_text(encoding="utf-8"))


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
        return json.loads(path.read_text(encoding="utf-8"))
    if paper_id in _lineaging:
        raise HTTPException(status_code=429, detail="脉络正在生成中")

    if req and req.config:
        from paper_reflow.llm import set_backend

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
    elif not os.getenv("PAPER_REFLOW_API_KEY"):
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


@app.post("/api/papers/{paper_id}/ask")
async def ask_paper(paper_id: str, req: AskReq) -> dict:
    """论文问答：先论文内检索，必要时升级到网络搜索，答案带出处。"""
    if not store.get(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    blocks = store.read_blocks(paper_id)
    if blocks is None:
        raise HTTPException(status_code=409, detail="还在解析中，请稍后再试")
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")

    if req.config:
        from paper_reflow.llm import set_backend

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
    elif not os.getenv("PAPER_REFLOW_API_KEY"):
        raise HTTPException(status_code=400, detail="未配置模型，无法回答")

    t0 = time.time()
    objs = [Block(**{**b, "bbox": tuple(b["bbox"])}) for b in blocks]
    docs, en, zh = qa_engine.build_index(objs)

    expanded = await qa_engine.expand_query(req.question)
    query = " ".join([req.question, *expanded])
    hits = qa_engine.retrieve(docs, en, zh, query, k=6)
    top_score = hits[0][1] if hits else 0.0

    web_results: list[dict] = []
    if req.web and qa_engine.needs_external(req.question, top_score):
        # 用英文关键词去搜学术内容，中文原问在通用搜索里几乎搜不到论文
        web_query = " ".join(expanded[:6]) if expanded else req.question
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
    text = await qa_engine.answer(req.question, context)

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

    return {
        "answer": text,
        "sources": sources,
        "usedWeb": bool(web_results),
        "latency": round(time.time() - t0, 1),
    }


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
        from paper_reflow.llm import set_backend

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
    from paper_reflow.llm import set_backend

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
        )
        log.info("解析完成 %s：%d 块", paper_id, len(blocks))
    except Exception as e:
        log.exception("解析失败 %s", paper_id)
        store.update(paper_id, status="failed", error=str(e)[:300])
    finally:
        _parsing.discard(paper_id)


# ── Zotero（实验性） ────────────────────────────────────────────────────

ZOTERO_BASE = os.getenv("PAPER_REFLOW_ZOTERO", "http://127.0.0.1:23119/api/users/0")


@app.get("/api/zotero/probe")
async def zotero_probe() -> dict:
    """探测本机 Zotero 7 的本地 API 是否开启。"""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
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
        async with httpx.AsyncClient(timeout=20) as c:
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

    async with httpx.AsyncClient(timeout=60) as c:
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


if WEB_DIST.exists():
    # 直接托管打包后的前端：python -m server.main 打开 http://localhost:8000 即可
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")


def main() -> None:
    import uvicorn

    port = int(os.getenv("PAPER_REFLOW_PORT", "8010"))
    uvicorn.run("server.main:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
