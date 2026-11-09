"""研究 agent 的内部工具接口：学术检索、入库、项目上下文、产物与标签。

与 ``search_api`` 一样只监听本机、用 ``x-internal-token`` 校验；区别是这里
直接拿得到研究项目库，所以工具可以按 ``sessionId`` 定位当前项目，模型不必
猜项目 id。

工具与接口的对应关系：
    academic_search  GET  /internal/research/scholar/search
    paper_import     POST /internal/research/scholar/import
    project_read     GET  /internal/research/sessions/{id}/project
    project_write    PATCH /internal/research/sessions/{id}/project
                     POST /internal/research/sessions/{id}/files
                     POST /internal/research/sessions/{id}/artifacts
    artifact_read    GET  /internal/research/sessions/{id}/artifacts
                     GET  /internal/research/artifacts/{artifactId}
    library_tag      GET  /internal/research/library/papers
                     PUT  /internal/research/papers/{paperId}/tags
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from paperead.scholar import search as scholar_search
from paperead.scholar.cite import build_bibtex
from paperead.scholar.http import FriendlyError
from paperead.scholar.models import paper_from_dict
from paperead.scholar.search import SOURCE_KEYS, SearchInput
from server.research.files import files_root, safe_name, save_bytes
from server.research.latex import (
    bib_stem,
    build_tex,
    compile_tex,
    find_pandoc,
    has_cjk,
    read_text,
)
from server.research.store import ResearchStore
from server.scholar_api import import_paper

MAX_READ_CHARS = 20000
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".bib", ".tex", ".yaml", ".yml"}
SOURCES = set(SOURCE_KEYS)


def _check(token: str | None) -> None:
    expected = os.environ.get("PAPEREAD_INTERNAL_TOKEN", "")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


class ProjectPatchIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    description: str | None = None
    research_question: str | None = Field(default=None, alias="researchQuestion")
    instructions: str | None = None


class FileIn(BaseModel):
    content: str
    title: str | None = None
    filename: str | None = None
    kind: str = "note"


class ArtifactIn(BaseModel):
    content: str
    title: str | None = None
    kind: str = "note"
    filename: str | None = None


class TagsIn(BaseModel):
    tags: list[str] = Field(default_factory=list)
    mode: str = "append"  # append | replace


class ImportIn(BaseModel):
    id: str = Field(..., min_length=3, max_length=300)


class ExportIn(BaseModel):
    format: str = "docx"  # docx | bib | latex | pdf
    source: str | None = None  # 项目文件名 / 产物名
    content: str | None = None
    title: str | None = None
    filename: str | None = None
    author: str | None = None
    abstract: str | None = None
    template: str | None = None  # auto | ctexart | article
    compile: bool = True  # latex 格式下是否顺带编译 PDF
    papers: list[str] = Field(default_factory=list)  # bib 用的 paperId


def _project_payload(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": project["id"],
        "kind": project["kind"],
        "name": project["name"],
        "description": project.get("description") or "",
        "researchQuestion": project.get("research_question") or "",
        "instructions": project.get("instructions") or "",
        "workspace": project["workspace"],
        "createdAt": project["created_at"],
        "updatedAt": project["updated_at"],
    }


def _artifact_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(row.get("meta") or "{}")
    except ValueError:
        meta = {}
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "sessionId": row.get("session_id"),
        "path": row.get("path") or "",
        "kind": row.get("kind") or "",
        "title": row.get("title") or "",
        "version": int(row.get("version") or 1),
        "createdAt": row["created_at"],
        "meta": meta if isinstance(meta, dict) else {},
    }


def _read_text(path: Path, limit: int = MAX_READ_CHARS) -> str:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def _export_source(
    research: ResearchStore, project_id: str, source: str | None
) -> tuple[str, str]:
    """找到要导出的 markdown 正文，返回 (标题, 文本)。

    ``source`` 可以是项目文件名、相对路径或产物标题；为空时取最近的 markdown
    产物 / 文件，方便“把当前综述导出”这类不带参数的说法。
    """
    workspace = Path(research.workspace(project_id))
    items = research.list_items(project_id)
    artifacts = research.list_artifacts(project_id)

    def _item_text(item: dict[str, Any]) -> tuple[str, str]:
        text = _read_text(research.item_path(project_id, item))
        return str(item.get("title") or ""), text

    def _artifact_text(row: dict[str, Any]) -> tuple[str, str]:
        path = workspace / "artifacts" / str(row.get("path") or "")
        return str(row.get("title") or ""), _read_text(path)

    if source:
        name = safe_name(source)
        for row in artifacts:
            if source in {row.get("title"), row.get("path")} or Path(
                str(row.get("path") or "")
            ).name == name:
                title, text = _artifact_text(row)
                if text:
                    return title, text
        for item in items:
            if source in {item.get("title"), item.get("path")} or Path(
                str(item.get("path") or "")
            ).name == name:
                title, text = _item_text(item)
                if text:
                    return title, text
        direct = _read_text(workspace / "artifacts" / name) or _read_text(
            workspace / "files" / name
        )
        if direct:
            return Path(name).stem, direct
        return "", ""
    for row in artifacts:  # list_artifacts 已按创建时间倒序
        if str(row.get("path") or "").lower().endswith((".md", ".txt")):
            title, text = _artifact_text(row)
            if text:
                return title, text
    for item in items:
        if str(item.get("path") or "").lower().endswith((".md", ".txt")):
            title, text = _item_text(item)
            if text:
                return title, text
    return "", ""


def _bib_dict(row: dict[str, Any]) -> dict[str, Any]:
    authors = row.get("authors") or []
    if isinstance(authors, str):
        authors = [
            part.strip()
            for part in authors.replace("，", ",").split(",")
            if part.strip()
        ]
    year = row.get("year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    return {
        "id": row.get("source_id") or row["id"],
        "source": row.get("source") or "openalex",
        "title": row.get("title") or "",
        "authors": authors,
        "venue": row.get("venue") or "",
        "year": year,
        "doi": row.get("doi") or None,
        "url": row.get("url") or "",
        "abstract": row.get("abstract") or "",
    }


def _find_source_path(
    research: ResearchStore, project_id: str, source: str | None
) -> Path | None:
    """按文件名 / 产物标题定位项目里的真实文件，用于“就地转 LaTeX / 编译”。"""
    if not source:
        return None
    workspace = Path(research.workspace(project_id))
    name = safe_name(source)
    for row in research.list_artifacts(project_id):
        if source in {row.get("title"), row.get("path")} or Path(
            str(row.get("path") or "")
        ).name == name:
            path = workspace / "artifacts" / str(row.get("path") or "")
            if path.is_file():
                return path
    for item in research.list_items(project_id):
        if source in {item.get("title"), item.get("path")} or Path(
            str(item.get("path") or "")
        ).name == name:
            path = research.item_path(project_id, item)
            if path.is_file():
                return path
    for candidate in (workspace / "files" / name, workspace / "artifacts" / name):
        if candidate.is_file():
            return candidate
    return None


def _export_latex(
    research: ResearchStore, project: dict[str, Any], body: ExportIn, fmt: str
) -> dict[str, Any]:
    """把 Markdown 稿转成 .tex，或在已有 .tex 上编译 PDF。

    ``format=latex`` 时即使没有 TeX 引擎也算成功（只给 warning）；
    ``format=pdf`` 时编译失败就是错误，避免模型谎称生成了 PDF。
    """
    pid = str(project["id"])
    workspace = Path(project["workspace"])
    source_path = _find_source_path(research, pid, body.source)
    title = (body.title or "").strip()
    text = body.content or ""

    if not text and source_path and source_path.suffix.lower() in {
        ".tex",
        ".md",
        ".markdown",
        ".txt",
    }:
        text = read_text(source_path)
    if not text.strip():
        found_title, text = _export_source(research, pid, body.source)
        title = title or found_title
    if not text.strip():
        return {
            "format": fmt,
            "files": [],
            "error": "没有找到要导出的正文：请传 content，或让 source 指向项目里的文稿。",
        }

    files: list[dict[str, Any]] = []
    source_root = files_root(workspace)
    # 只有项目文件树里的 .tex 才就地编译；产物里的 .tex 复制一份到 files/ 再编译，
    # 保证新增条目的 path 始终是 files/ 内的相对路径。
    existing_tex = (
        source_path
        if source_path is not None
        and source_path.suffix.lower() == ".tex"
        and source_root in source_path.resolve().parents
        else None
    )

    if existing_tex is not None and not body.content:
        tex_path = existing_tex
        style = "ctexart" if has_cjk(text) else "article"
        title = title or existing_tex.stem
        relative = tex_path.resolve().relative_to(source_root).as_posix()
        files.append(
            {
                "name": tex_path.name,
                "path": relative,
                "size": tex_path.stat().st_size,
                "kind": "tex",
            }
        )
    else:
        target_dir = source_path.parent if existing_tex is not None else source_root
        bib = bib_stem(target_dir) or bib_stem(source_root) or bib_stem(workspace)
        tex, style, error = build_tex(
            text,
            title=title,
            author=body.author or "",
            abstract=body.abstract or "",
            template=body.template or "auto",
            bib_name=bib,
        )
        if error:
            return {"format": fmt, "files": [], "error": error}
        # 默认 main.tex：ASCII 文件名能让 bibtex 在 Windows 上稳定工作。
        filename = safe_name(body.filename or "main.tex")
        if not filename.lower().endswith(".tex"):
            filename += ".tex"
        relative, size = save_bytes(workspace, filename, tex.encode("utf-8"))
        item = research.add_item(
            pid, "file", filename, path=relative, mime="text/x-tex", size=size
        )
        files.append(
            {
                "name": filename,
                "path": relative,
                "size": size,
                "itemId": item["id"],
                "kind": "tex",
            }
        )
        tex_path = source_root / relative

    result: dict[str, Any] = {
        "format": fmt,
        "files": files,
        "style": style,
        "source": title,
        "compiled": False,
    }

    if fmt == "latex" and not body.compile:
        return result

    pdf, engine, error = compile_tex(tex_path)
    if pdf is not None and pdf.is_file():
        relative_pdf = pdf.relative_to(source_root).as_posix()
        size = pdf.stat().st_size
        item = research.add_item(
            pid, "file", pdf.name, path=relative_pdf, mime="application/pdf", size=size
        )
        files.append(
            {
                "name": pdf.name,
                "path": relative_pdf,
                "size": size,
                "itemId": item["id"],
                "kind": "pdf",
            }
        )
        result["compiled"] = True
        result["engine"] = Path(engine).name
        return result

    # .tex 已生成：latex 只是“没编译成 PDF”的提醒；pdf 才是失败。
    if fmt == "pdf":
        result["error"] = error
    else:
        result["warning"] = error
    return result


def build_router(
    research: ResearchStore,
    library: Any,
    add_pdf: Callable[[str, bytes, dict[str, Any]], dict[str, Any]],
    public: Callable[[dict[str, Any]], dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/internal/research", tags=["research-internal-tools"])

    def _session_project(session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        session = research.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        project = research.get_project(str(session["project_id"]))
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return session, project

    # ── 学术检索 / 入库 ────────────────────────────────

    @router.get("/scholar/search")
    async def scholar_query(
        q: str = Query(default="", max_length=300),
        source: str = Query(default="openalex"),
        year_from: int | None = Query(default=None, alias="yearFrom"),
        year_to: int | None = Query(default=None, alias="yearTo"),
        limit: int = Query(default=10, ge=1, le=30),
        abstract_only: bool = Query(default=True, alias="abstractOnly"),
        abstract_chars: int = Query(default=700, ge=200, le=2000, alias="abstractMaxChars"),
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        if not q.strip():
            return {"papers": [], "total": 0, "error": "query is empty"}
        chosen = source if source in SOURCES else "openalex"
        # 默认只返回带摘要的记录，并多取一批以便筛掉无摘要题录后仍有足够候选。
        fetch_limit = min(limit * 2, 30) if abstract_only else limit
        try:
            result, used_query, translated = await scholar_search.search(
                SearchInput(
                    q=q.strip(),
                    source=chosen,
                    from_year=year_from,
                    to_year=year_to,
                    per_page=fetch_limit,
                )
            )
        except FriendlyError as exc:
            return {"papers": [], "total": 0, "error": exc.message}
        except Exception as exc:  # 数据源结构变化不能把工具打挂
            return {"papers": [], "total": 0, "error": f"检索失败：{exc}"}
        raw: list[dict[str, Any]] = []
        for paper in result.papers:
            item = paper.to_dict()
            item["abstract"] = (item.get("abstract") or "").strip()[:abstract_chars]
            item["hasAbstract"] = bool(item["abstract"])
            item["authors"] = (item.get("authors") or [])[:8]
            raw.append(item)
        papers = [item for item in raw if item["hasAbstract"]] if abstract_only else raw
        notice = ""
        if abstract_only and not papers and raw:
            # 宁可退回题录也不要让用户看到空结果；由模型标注“仅题录”。
            papers = raw
            notice = "这批结果没有可用的摘要，已退回题录；请只按标题做保守判断，或换关键词/数据源。"
        papers = papers[:limit]
        if not papers and not notice:
            notice = "没有符合条件的结果（默认已过滤无摘要记录）；建议缩短关键词、换数据源，或把 abstractOnly 设为 false。"
        return {
            "papers": papers,
            "total": result.total,
            "returned": len(papers),
            "withAbstract": sum(1 for item in papers if item["hasAbstract"]),
            "source": chosen,
            "usedQuery": used_query,
            "translated": translated,
            "notice": notice,
            "error": None,
        }

    @router.post("/scholar/import")
    async def scholar_import(
        body: ImportIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        try:
            return await import_paper(library, add_pdf, public, body.id)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return {"imported": False, "already": False, "error": detail}

    @router.post("/sessions/{session_id}/papers/import")
    async def project_import(
        session_id: str,
        body: ImportIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        """把学术检索结果放进**当前项目自己的论文文件夹**，不进全局文献库。

        全局库里已有同一篇时直接复用，避免重复下载；项目文件树里仍会出现一条
        ``kind=paper`` 的条目，可读、可删除，右键可加入文献库。
        """
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        pid = str(project["id"])
        global_row = library.find_by_source(body.id, project_id=None)
        imported = False
        already = False
        if global_row is not None:
            row = global_row
            already = True
        else:
            try:
                result = await import_paper(
                    library, add_pdf, public, body.id, project_id=pid
                )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                return {"imported": False, "already": False, "error": detail}
            if result.get("error"):
                return {"imported": False, "already": False, "error": result["error"]}
            paper_id = str((result.get("paper") or {}).get("id") or "")
            row = library.get(paper_id) if paper_id else None
            imported = bool(result.get("imported"))
            already = bool(result.get("already"))
        if row is None:
            return {"imported": imported, "already": already, "error": "导入后找不到文献记录。"}

        paper_id = str(row["id"])
        existing_item = next(
            (item for item in research.list_items(pid) if item.get("ref_id") == paper_id),
            None,
        )
        item = existing_item or research.add_item(
            pid,
            "paper",
            row.get("title") or "论文",
            path="",
            ref_id=paper_id,
            mime="application/pdf",
            size=int(row.get("size") or 0),
            meta={"projectScoped": (row.get("project_id") or None) == pid},
        )
        return {
            "paper": public(row),
            "item": {"id": item["id"], "name": item["title"], "path": item.get("path") or ""},
            "imported": imported,
            "already": already or bool(existing_item),
            "projectScoped": (row.get("project_id") or None) == pid,
        }

    # ── 项目上下文 ─────────────────────────────────────

    @router.get("/sessions/{session_id}/project")
    def read_project(
        session_id: str,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        session, project = _session_project(session_id)
        rows = library.list()
        tags: list[str] = []
        for row in rows:
            for tag in row.get("tags") or []:
                if tag not in tags:
                    tags.append(str(tag))
        return {
            "session": {
                "id": session["id"],
                "title": session["title"],
                "status": session["status"],
            },
            "project": _project_payload(project),
            "files": [
                {
                    "name": item["title"],
                    "path": item.get("path") or "",
                    "kind": item["kind"],
                    "paperId": item.get("ref_id") or None,
                    "size": int(item.get("size") or 0),
                }
                for item in research.list_items(str(project["id"]))
            ],
            "artifacts": [
                _artifact_payload(row)
                for row in research.list_artifacts(str(project["id"]))
            ],
            "sessions": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "updatedAt": row["updated_at"],
                }
                for row in research.list_sessions(str(project["id"]))[:20]
            ],
            "library": {"papers": len(rows), "tags": tags[:50]},
        }

    @router.patch("/sessions/{session_id}/project")
    def patch_project(
        session_id: str,
        body: ProjectPatchIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        patch = {
            key: value
            for key, value in body.model_dump(exclude_none=True).items()
        }
        if not patch:
            return {"project": _project_payload(project)}
        updated = research.update_project(str(project["id"]), **patch)
        if updated is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {"project": _project_payload(updated)}

    # ── 项目文件 / 产物 ────────────────────────────────

    @router.post("/sessions/{session_id}/files")
    def write_file(
        session_id: str,
        body: FileIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        workspace = Path(project["workspace"])
        fallback = (body.title or "未命名").strip() + ".md"
        filename = safe_name(body.filename or fallback)
        relative, size = save_bytes(workspace, filename, body.content.encode("utf-8"))
        kind = body.kind if body.kind in {"file", "note"} else "note"
        item = research.add_item(
            str(project["id"]),
            kind,
            body.title or filename,
            path=relative,
            mime="text/markdown" if filename.endswith(".md") else "text/plain",
            size=size,
        )
        return {
            "file": {
                "id": item["id"],
                "name": item["title"],
                "path": relative,
                "size": size,
            }
        }

    def _write_artifact(
        project: dict[str, Any], session_id: str, body: ArtifactIn
    ) -> dict[str, Any]:
        workspace = Path(project["workspace"])
        target_dir = workspace / "artifacts"
        target_dir.mkdir(parents=True, exist_ok=True)
        fallback = (body.title or "未命名产物").strip() + ".md"
        filename = safe_name(body.filename or fallback)
        target = target_dir / filename
        if target.exists():
            stem, suffix = target.stem, target.suffix
            for index in range(1, 1000):
                candidate = target_dir / f"{stem}-{index}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
        target.write_text(body.content, encoding="utf-8")
        row = research.add_artifact(
            str(project["id"]),
            body.title or target.stem,
            body.kind or "note",
            target.name,
            session_id=session_id,
        )
        return _artifact_payload(row)

    @router.post("/sessions/{session_id}/artifacts")
    def write_artifact(
        session_id: str,
        body: ArtifactIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        return {"artifact": _write_artifact(project, session_id, body)}

    @router.get("/sessions/{session_id}/artifacts")
    def list_artifacts(
        session_id: str,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        return {
            "artifacts": [
                _artifact_payload(row)
                for row in research.list_artifacts(str(project["id"]))
            ]
        }

    @router.get("/artifacts/{artifact_id}")
    def read_artifact(
        artifact_id: str,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        row = research.get_artifact(artifact_id)
        if row is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        project = research.get_project(str(row["project_id"]))
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        path = Path(project["workspace"]) / "artifacts" / str(row.get("path") or "")
        text = _read_text(path)
        return {
            "artifact": _artifact_payload(row),
            "content": text,
            "binary": not text,
        }

    # ── 文献库标签 ─────────────────────────────────────

    @router.get("/library/papers")
    def list_papers(
        limit: int = Query(default=50, ge=1, le=200),
        paper_id: str | None = Query(default=None, alias="paperId"),
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        rows = library.list()
        if paper_id:
            rows = [row for row in rows if row["id"] == paper_id]
        return {
            "papers": [
                {
                    "paperId": row["id"],
                    "title": row.get("title") or "",
                    "authors": row.get("authors") or "",
                    "pages": int(row.get("pages") or 0),
                    "status": row.get("status") or "",
                    "tags": [str(tag) for tag in row.get("tags") or []],
                }
                for row in rows[:limit]
            ]
        }

    @router.put("/papers/{paper_id}/tags")
    def update_tags(
        paper_id: str,
        body: TagsIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        row = library.get(paper_id)
        if row is None:
            raise HTTPException(status_code=404, detail="paper not found")
        incoming = [str(tag).strip() for tag in body.tags if str(tag).strip()]
        if body.mode == "replace":
            tags = incoming
        else:
            tags = list(row.get("tags") or [])
            for tag in incoming:
                if tag not in tags:
                    tags.append(tag)
        updated = library.update(paper_id, tags=tags[:20])
        if updated is None:
            raise HTTPException(status_code=404, detail="paper not found")
        return {"paperId": paper_id, "tags": [str(tag) for tag in updated.get("tags") or []]}

    # ── 导出 docx / bib ────────────────────────────────

    @router.post("/sessions/{session_id}/export")
    def export_document(
        session_id: str,
        body: ExportIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        """把项目里的 markdown 综述导出为 docx，或把指定文献导出为 BibTeX。"""
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        pid = str(project["id"])
        workspace = Path(project["workspace"])
        fmt = (body.format or "docx").strip().lower()
        if fmt == "tex":
            fmt = "latex"

        if fmt == "bib":
            rows = [library.get(paper_id) for paper_id in body.papers]
            found = [row for row in rows if row is not None]
            if not found:
                return {
                    "format": "bib",
                    "files": [],
                    "error": "没有可导出的文献：请在 papers 里传入 paperId（来自检索结果）。",
                }
            text = build_bibtex([paper_from_dict(_bib_dict(row)) for row in found])
            filename = safe_name(body.filename or "references.bib")
            if not filename.lower().endswith(".bib"):
                filename += ".bib"
            relative, size = save_bytes(workspace, filename, text.encode("utf-8"))
            item = research.add_item(
                pid, "file", filename, path=relative, mime="text/plain", size=size
            )
            return {
                "format": "bib",
                "files": [
                    {"name": filename, "path": relative, "size": size, "itemId": item["id"]}
                ],
                "entries": len(found),
            }

        if fmt in {"latex", "pdf"}:
            return _export_latex(research, project, body, fmt)

        if fmt != "docx":
            return {"format": fmt, "files": [], "error": "只支持 docx、bib、latex、pdf 四种格式。"}

        title, text = _export_source(research, pid, body.source)
        if body.content:
            text = body.content
        if not text.strip():
            return {
                "format": "docx",
                "files": [],
                "error": "没有找到要导出的正文：请传 content，或 source 指向项目里的 markdown 文件。",
            }
        exe = find_pandoc()
        if not exe:
            return {
                "format": "docx",
                "files": [],
                "error": "没有找到可用的 pandoc，无法生成 docx；可改用 bib 或 markdown。",
            }
        filename = safe_name(body.filename or f"{body.title or title or 'review'}.docx")
        if not filename.lower().endswith(".docx"):
            filename += ".docx"
        tmp = workspace / f".export-{os.getpid()}.docx"
        try:
            proc = subprocess.run(
                [exe, "-f", "markdown", "-t", "docx", "-o", str(tmp), "-"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            tmp.unlink(missing_ok=True)
            return {"format": "docx", "files": [], "error": f"pandoc 执行失败：{exc}"}
        if proc.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            detail = proc.stderr.decode("utf-8", "replace")[:300]
            return {"format": "docx", "files": [], "error": f"pandoc 转换失败：{detail}"}
        data = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
        relative, size = save_bytes(workspace, filename, data)
        item = research.add_item(
            pid,
            "file",
            filename,
            path=relative,
            mime=(
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
            size=size,
        )
        return {
            "format": "docx",
            "files": [{"name": filename, "path": relative, "size": size, "itemId": item["id"]}],
            "bytes": size,
            "source": title or body.source or "",
        }

    return router
