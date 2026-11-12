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
    docx_read        GET  /internal/research/sessions/{id}/docx
    docx_edit        POST /internal/research/sessions/{id}/docx
"""

from __future__ import annotations

import json
import os
import re
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
from server.research import explorer, ppt_templates, templates as template_store
from server.research.citations import audit_review
from server.research.deck import build_deck, parse_outline
from server.research.docx_edit import DocxError, edit_docx, read_docx
from server.research.files import safe_name, save_bytes_in
from server.research.latex import (
    bib_stem,
    build_tex,
    compile_tex,
    find_pandoc,
    has_cjk,
    read_text,
)
from server.research.review_standard import BUILTIN_TEMPLATES
from server.research.review_standard import evaluate as evaluate_review
from server.research.review_standard import list_templates, resolve_template
from server.research.store import ResearchStore
from server.research.terminal import run_command
from server.scholar_api import import_paper

MAX_READ_CHARS = 20000
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".bib", ".tex", ".yaml", ".yml"}
SOURCES = set(SOURCE_KEYS)
CITATION_REPORT_NAME = "citation-report.md"


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
    review_template: str | None = Field(default=None, alias="reviewTemplate")


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


class DocxEditIn(BaseModel):
    """docx_edit 的入参：目标文件 + 一串按顺序执行的操作。"""

    path: str = Field(..., min_length=1)
    operations: list[dict[str, Any]] = Field(default_factory=list)


class ImportIn(BaseModel):
    id: str = Field(..., min_length=3, max_length=300)


class RunIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    command: str
    cwd: str | None = None
    timeout_ms: int | None = Field(default=120_000, alias="timeoutMs")


class ExportIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    format: str = "docx"  # docx | bib | latex | pdf | pptx
    source: str | None = None  # 项目文件名 / 产物名
    content: str | None = None
    title: str | None = None
    subtitle: str | None = None
    filename: str | None = None
    author: str | None = None
    date: str | None = None
    abstract: str | None = None
    template: str | None = None  # auto | ctexart | article
    compile: bool = True  # latex 格式下是否顺带编译 PDF
    papers: list[str] = Field(default_factory=list)  # bib 用的 paperId
    deck: list[dict[str, Any]] | None = None  # pptx 用的结构化幻灯片
    ppt_template: str | None = Field(default=None, alias="pptTemplate")  # 用户上传的 .pptx 模板


def _read_review_template(research: ResearchStore, project_id: str) -> str:
    """读项目级“综述模板”选择（存在 project.json，不占数据库列）。"""
    path = Path(research.workspace(project_id)) / "project.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = meta.get("review_template") if isinstance(meta, dict) else ""
    return str(value or "")


def _write_review_template(
    research: ResearchStore, project_id: str, value: str
) -> None:
    path = Path(research.workspace(project_id)) / "project.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    if value:
        meta["review_template"] = value
    else:
        meta.pop("review_template", None)
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _global_template_root(research: ResearchStore) -> Path:
    """``data/research`` → ``data/review-templates``。"""
    return template_store.root_for(Path(research.root))


def _effective_template_choice(
    research: ResearchStore, project_id: str, explicit: str | None = None
) -> str:
    """本次指定 > 项目设置 > 应用设置 > 系统默认。"""
    if explicit is not None:
        return explicit
    project_choice = _read_review_template(research, project_id)
    if project_choice:
        return project_choice
    return template_store.read_choice(_global_template_root(research)) or "default"


def _resolve_review_template(
    choice: str | None,
    files_dir: Path,
    research: ResearchStore,
    library: Any,
) -> tuple[list[str] | None, str, str]:
    root = _global_template_root(research)
    return resolve_template(
        choice,
        files_dir,
        external=lambda value: template_store.resolve_global(value, root, library),
    )


def _project_payload(
    project: dict[str, Any], research: ResearchStore, library: Any = None
) -> dict[str, Any]:
    project_template = _read_review_template(research, str(project["id"]))
    global_template = template_store.read_choice(_global_template_root(research))
    template = project_template or global_template or "default"
    # 模板详情（章节骨架 + 每节约束 + 图表/语言指南）一次解析后一起给 agent，
    # 避免 agent 只拿到 id 却不知道每一节该写什么。
    detail = template_store.describe(
        template, _global_template_root(research), library
    )
    return {
        "id": project["id"],
        "kind": project["kind"],
        "name": project["name"],
        "description": project.get("description") or "",
        "researchQuestion": project.get("research_question") or "",
        "instructions": project.get("instructions") or "",
        # 项目没设时回落到应用设置，agent 不用再猜当前该用哪套骨架。
        "reviewTemplate": template,
        "reviewTemplateSource": (
            "project" if project_template else "global" if global_template else "default"
        ),
        # 论文/综述模板的类型、章节、每节约束与完整 Markdown；upload/paper 也会有。
        "reviewTemplateKind": detail.get("kind") or "review",
        "reviewTemplateOutline": detail.get("outline") or [],
        "reviewTemplateSections": detail.get("sections") or [],
        "reviewTemplateMarkdown": detail.get("markdown") or "",
        # 论文模板的结构、图表放置与语言风格；upload/default 时为空。
        "reviewTemplateGuide": detail.get("guide") or {},
        "workspace": project["workspace"],
        # 用户选择的本地工作文件夹；为空时项目文件在 workspace/files。
        "rootPath": project.get("root_path") or "",
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
    artifact_dir = research.artifacts_root(project_id)
    legacy_dir = research.legacy_artifacts_root(project_id)
    root = Path(research.workspace_root(project_id))
    items = research.list_items(project_id)
    artifacts = research.list_artifacts(project_id)

    def _item_text(item: dict[str, Any]) -> tuple[str, str]:
        text = _read_text(research.item_path(project_id, item))
        return str(item.get("title") or ""), text

    def _artifact_text(row: dict[str, Any]) -> tuple[str, str]:
        rel = str(row.get("path") or "")
        path = artifact_dir / rel
        if not path.is_file():
            # 旧版产物仍可能留在内部项目目录
            path = legacy_dir / rel
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
        direct = (
            _read_text(root / name)
            or _read_text(artifact_dir / name)
            or _read_text(legacy_dir / name)
        )
        if direct:
            return Path(name).stem, direct
        return "", ""
    for row in artifacts:  # list_artifacts 已按创建时间倒序
        if Path(str(row.get("path") or "")).name == CITATION_REPORT_NAME:
            continue
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
    if year is None:
        guessed = _publication_year(row)
        year = int(guessed) if guessed.isdigit() else None
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


def _first_surname(authors: Any) -> str:
    """从 “Katherine Berry / Zhang B, Bian S” 里取第一作者姓。"""
    if isinstance(authors, list):
        first = str(authors[0] if authors else "")
    else:
        first = str(authors or "").split(",")[0]
    parts = first.strip().split()
    if not parts:
        return ""
    # “Zhang B” 这种姓在前、缩写在后的写法，取第一个词。
    if len(parts) >= 2 and re.fullmatch(r"[A-Z][A-Za-z.\-]{0,2}", parts[-1]):
        return parts[0]
    return parts[-1]


def _publication_year(row: dict[str, Any]) -> str:
    year = row.get("year")
    if year not in (None, ""):
        return str(year)
    # 文献库多数条目没有 year，只能从 arXiv 编号或 DOI 推断：
    # arxiv:2509.07887 / arxiv.org/abs/2610.24124 → 2025 / 2026
    # 10.1038/s41467-024-47120-y → 2024（Nature 系 DOI 用三位年份）
    text = " ".join(
        str(row.get(key) or "") for key in ("source_id", "url", "doi", "filename")
    )
    match = re.search(r"arxiv[.:/](?:abs/)?(\d{2})\d{2}\.\d{4,5}", text, re.IGNORECASE)
    if match:
        return f"20{match.group(1)}"
    match = re.search(r"-(0\d{2})-\d{4,5}", text)
    if match:
        return str(2000 + int(match.group(1)))
    match = re.search(r"(?:19|20)\d{2}", str(row.get("filename") or ""))
    return match.group(0) if match else ""


def _citation_labels(text: str, library: Any) -> dict[str, str]:
    """paperId → “作者 年份”，供 PDF 导出替换内部主键。"""
    labels: dict[str, str] = {}
    if library is None:
        return labels
    for row in _cited_rows(text, library):
        paper_id = str(row.get("id") or "")
        if not paper_id:
            continue
        surname = _first_surname(row.get("authors"))
        year = _publication_year(row)
        label = " ".join(part for part in (surname, year) if part)
        if label:
            labels[paper_id] = label
    return labels


def _find_source_path(
    research: ResearchStore, project_id: str, source: str | None
) -> Path | None:
    """按文件名 / 产物标题定位项目里的真实文件，用于“就地转 LaTeX / 编译”。"""
    if not source:
        return None
    workspace = Path(research.workspace(project_id))
    artifact_dir = research.artifacts_root(project_id)
    legacy_dir = research.legacy_artifacts_root(project_id)
    root = Path(research.workspace_root(project_id))
    name = safe_name(source)
    for row in research.list_artifacts(project_id):
        if source in {row.get("title"), row.get("path")} or Path(
            str(row.get("path") or "")
        ).name == name:
            rel = str(row.get("path") or "")
            path = artifact_dir / rel
            if not path.is_file():
                path = legacy_dir / rel
            if path.is_file():
                return path
    for item in research.list_items(project_id):
        if source in {item.get("title"), item.get("path")} or Path(
            str(item.get("path") or "")
        ).name == name:
            path = research.item_path(project_id, item)
            if path.is_file():
                return path
    for candidate in (root / name, artifact_dir / name, legacy_dir / name):
        if candidate.is_file():
            return candidate
    return None


_PPT_SUFFIXES = {".pptx", ".potx"}


def _find_ppt_template(
    research: ResearchStore, project_id: str, source: str | None
) -> tuple[Path | None, str]:
    """在项目文件里找用户上传的 PPT 模板，返回 (路径, 展示名)。"""
    if not source:
        return None, ""
    workspace = Path(research.workspace(project_id))
    root = Path(research.workspace_root(project_id))
    name = safe_name(source)
    for item in research.list_items(project_id):
        path = research.item_path(project_id, item)
        if path is None or path.suffix.lower() not in _PPT_SUFFIXES:
            continue
        if source in {item.get("title"), item.get("path")} or path.name == name:
            return path, str(item.get("title") or path.name)
    for candidate in (
        root / name,
        research.artifacts_root(project_id) / name,
        research.legacy_artifacts_root(project_id) / name,
    ):
        if candidate.is_file() and candidate.suffix.lower() in _PPT_SUFFIXES:
            return candidate, candidate.name
    return None, ""


_PAPER_ID_RE = re.compile(r"\bp_[0-9a-f]{10,16}\b")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)\]），。；、]+")


def _cited_rows(text: str, library: Any) -> list[dict[str, Any]]:
    """从正文里找出被引用的 paperId / DOI，用于自动生成参考文献表。"""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for paper_id in _PAPER_ID_RE.findall(text):
        if paper_id in seen:
            continue
        row = library.get(paper_id)
        if row:
            rows.append(row)
            seen.add(paper_id)

    dois = {match.rstrip(".").lower() for match in _DOI_RE.findall(text)}
    if dois:
        for row in library.list(include_project=True):
            doi = str(row.get("doi") or "").strip().lower()
            if doi and doi in dois and row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])
    return rows


def _export_pptx(
    research: ResearchStore, project: dict[str, Any], body: ExportIn
) -> dict[str, Any]:
    """把结构化 slides（或 markdown 提纲）渲染成 16:9 的 .pptx。"""
    pid = str(project["id"])
    workspace = Path(research.workspace_root(pid))
    title = (body.title or "").strip()
    slides = [item for item in (body.deck or []) if isinstance(item, dict)]

    if not slides:
        _found, text = _export_source(research, pid, body.source)
        if body.content:
            text = body.content
        if not text.strip():
            return {
                "format": "pptx",
                "files": [],
                "error": "没有可用的提纲：请传 deck（结构化 slides）或 source / content。",
            }
        slides = parse_outline(text, title=title)
        title = title or str(slides[0].get("title") or "")

    if body.subtitle and slides:
        slides[0].setdefault("subtitle", body.subtitle)
    if body.author:
        slides[0].setdefault("author", body.author)
    if body.date:
        slides[0].setdefault("date", body.date)

    template_bytes: bytes | None = None
    template_label = "builtin"
    if body.ppt_template:
        template_path, template_label = _find_ppt_template(
            research, pid, body.ppt_template
        )
        if template_path is None:
            return {
                "format": "pptx",
                "files": [],
                "error": (
                    f"没有找到 PPT 模板：{body.ppt_template}。"
                    "请先把 .pptx 上传到项目文件树，再用文件名指定。"
                ),
            }
        template_bytes = template_path.read_bytes()
    else:
        template_bytes, template_label = ppt_templates.resolve(
            _global_template_root(research)
        )

    try:
        blob = build_deck(slides, title=title, template=template_bytes)
    except Exception as error:  # noqa: BLE001 - 渲染失败要回显原因
        return {"format": "pptx", "files": [], "error": f"生成 PPT 失败：{error}"}

    filename = safe_name(body.filename or f"{title or '汇报'}.pptx")
    if not filename.lower().endswith(".pptx"):
        filename += ".pptx"
    relative, size = save_bytes_in(workspace, filename, blob)
    item = research.add_item(
        pid,
        "file",
        filename,
        path=relative,
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size=size,
    )
    return {
        "format": "pptx",
        "files": [
            {
                "name": filename,
                "path": relative,
                "size": size,
                "itemId": item["id"],
                "kind": "pptx",
            }
        ],
        "slides": len(slides),
        "title": title,
        "template": template_label,
    }


def _export_latex(
    research: ResearchStore,
    project: dict[str, Any],
    body: ExportIn,
    fmt: str,
    audit: Callable[[str], dict[str, Any]] | None = None,
    library: Any | None = None,
) -> dict[str, Any]:
    """把 Markdown 稿转成 .tex，或在已有 .tex 上编译 PDF。

    ``format=latex`` 时即使没有 TeX 引擎也算成功（只给 warning）；
    ``format=pdf`` 时编译失败就是错误，避免模型谎称生成了 PDF。
    """
    pid = str(project["id"])
    workspace = Path(project["workspace"])
    root = Path(research.workspace_root(pid))
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

    # 已经是完整 .tex 的稿件不做 Markdown 引用审计，避免把 \cite 当成 0 引用。
    citation = (
        audit(text)
        if audit is not None and not text.lstrip().startswith("\\documentclass")
        else None
    )
    files: list[dict[str, Any]] = []
    source_root = root
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
        nocite = False
        # 没有 .bib 时，按正文里出现的 paperId / DOI 自动生成一份，
        # 让 PDF 至少有一份可核对的参考文献表。
        if bib is None and library is not None:
            cited = _cited_rows(text, library)
            if cited:
                bib_text = build_bibtex(
                    [paper_from_dict(_bib_dict(row)) for row in cited]
                )
                bib_file = target_dir / "references.bib"
                bib_file.write_text(bib_text, encoding="utf-8")
                bib_relative = bib_file.resolve().relative_to(source_root).as_posix()
                research.add_item(
                    pid,
                    "file",
                    "references.bib",
                    path=bib_relative,
                    mime="text/plain",
                    size=bib_file.stat().st_size,
                )
                files.append(
                    {
                        "name": "references.bib",
                        "path": bib_relative,
                        "size": bib_file.stat().st_size,
                        "kind": "bib",
                    }
                )
                bib = "references"
                nocite = True
        tex, style, error = build_tex(
            text,
            title=title,
            author=body.author or "",
            abstract=body.abstract or "",
            template=body.template or "auto",
            bib_name=bib,
            nocite=nocite,
            citation_labels=_citation_labels(text, library) if library is not None else None,
        )
        if error:
            if find_pandoc() is None:
                # 导出时才发现 pandoc 没了：作废设置页缓存，下次打开重新检测。
                from server.toolchain_api import invalidate_cache

                invalidate_cache()
            return {"format": fmt, "files": [], "error": error}
        # 默认 main.tex：ASCII 文件名能让 bibtex 在 Windows 上稳定工作。
        filename = safe_name(body.filename or "main.tex")
        if not filename.lower().endswith(".tex"):
            filename += ".tex"
        # bibtex 对带点的 jobname 容易失败，统一把主干里的点换成短横线。
        filename = f"{Path(filename).stem.replace('.', '-')}.tex"
        relative, size = save_bytes_in(root, filename, tex.encode("utf-8"))
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
    if citation is not None:
        result["citation"] = citation

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
    if error and "未检测到 LaTeX 引擎" in error:
        # 引擎被卸载或 PATH 变化：作废设置页缓存，下次打开重新检测。
        from server.toolchain_api import invalidate_cache

        invalidate_cache()
    return result


_ISSUE_LABELS = {
    "unresolved_paper": "未解析引用",
    "page_unverifiable": "页码无法核对",
    "page_out_of_range": "页码超出范围",
    "weak_support": "弱支持",
    "uncited_claim": "无引用论断",
}


def _citation_markdown(report: dict[str, Any], label: str) -> str:
    metrics = report.get("metrics") or {}
    lines = [
        f"# 引用审计 · {label}",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 引用总数 | {metrics.get('citationsTotal', 0)} |",
        f"| 本地引用 | {metrics.get('localCitations', 0)} |",
        f"| 页码级引用 | {metrics.get('pageLevel', 0)} |",
        f"| 页码级引用率 | {round(float(metrics.get('pageRatio') or 0) * 100)}% |",
        f"| 来源数 | {metrics.get('sourcesUnique', 0)} |",
        f"| 有页码来源 | {metrics.get('sourcesWithPage', 0)} |",
        f"| 全文来源 | {metrics.get('fulltextSources', 0)} |",
        f"| 未解析引用 | {metrics.get('unresolved', 0)} |",
        f"| 弱支持 | {metrics.get('weakSupport', 0)} |",
        f"| 无引用论断 | {metrics.get('uncitedClaims', 0)} |",
        f"| 分节 | {metrics.get('sections', 0)}"
        f"（来源不足 2 篇：{metrics.get('weakSections', 0)}） |",
        "",
        "## 建议",
        "",
    ]
    lines.extend(f"- {item}" for item in report.get("suggestions") or [])
    issues = report.get("issues") or []
    if issues:
        lines.extend(["", "## 明细", ""])
        for issue in issues[:30]:
            label_text = _ISSUE_LABELS.get(str(issue.get("kind")), str(issue.get("kind")))
            where = f"第 {issue.get('line')} 行"
            if issue.get("paperId"):
                where += f" · {issue['paperId']}"
                if issue.get("page"):
                    where += f" p{issue['page']}"
            detail = str(issue.get("detail") or "")
            claim = str(issue.get("claim") or "").strip()
            suffix = f"「{claim}」" if claim else ""
            lines.append(f"- {where} {label_text}：{detail}{suffix}")
    return "\n".join(lines) + "\n"


def _audit_export(
    research: ResearchStore,
    library: Any,
    project: dict[str, Any],
    text: str,
    label: str,
    session_id: str,
) -> dict[str, Any]:
    """导出前跑一次引用审计，并把报告写成项目产物。"""
    project_id = str(project["id"])
    report = audit_review(text, project_id, library)
    metrics = report.get("metrics") or {}
    warnings: list[str] = []
    if metrics.get("unresolved"):
        warnings.append(
            f"{metrics['unresolved']} 处引用无法解析：paperId 不在本地库或页码超出全文范围。"
        )
    if float(metrics.get("pageRatio") or 0) < 0.6:
        warnings.append(
            f"页码级引用率 {round(float(metrics['pageRatio']) * 100)}%，低于 60%："
            "关键论断需要先 paper_read 再标注页码。"
        )
    if metrics.get("uncitedClaims"):
        warnings.append(f"{metrics['uncitedClaims']} 处结论句没有引用。")

    target_dir = research.artifacts_root(project_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / CITATION_REPORT_NAME
    markdown = _citation_markdown(report, label or "综述草稿")
    try:
        target.write_text(markdown, encoding="utf-8")
        registered = {
            str(row.get("path") or "")
            for row in research.list_artifacts(project_id)
        }
        if CITATION_REPORT_NAME not in registered:
            research.add_artifact(
                project_id,
                "引用审计报告",
                "note",
                CITATION_REPORT_NAME,
                session_id=session_id,
                meta={"metrics": metrics},
            )
    except OSError:
        pass

    return {
        "metrics": metrics,
        "warnings": warnings,
        "report": f"artifacts/{CITATION_REPORT_NAME}",
        "suggestions": report.get("suggestions") or [],
    }


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
        project_id = str(project["id"])
        if str(project.get("root_path") or "").strip():
            files = explorer.project_files(Path(research.workspace_root(project_id)))
        else:
            files = [
                {
                    "name": item["title"],
                    "path": item.get("path") or "",
                    "kind": item["kind"],
                    "paperId": item.get("ref_id") or None,
                    "size": int(item.get("size") or 0),
                }
                for item in research.list_items(project_id)
            ]
        return {
            "session": {
                "id": session["id"],
                "title": session["title"],
                "status": session["status"],
            },
            "project": _project_payload(project, research, library),
            "files": files,
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

    @router.get("/sessions/{session_id}/files")
    def read_file(
        session_id: str,
        path: str = Query(..., description="project_read 返回的相对路径或文件名"),
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        """读取当前项目里的文本文件，供 agent 在后续轮次检查/修订草稿。"""
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        project_id = str(project["id"])
        wanted = path.strip()
        root = Path(research.workspace_root(project_id))
        if str(project.get("root_path") or "").strip():
            name = safe_name(wanted)
            entries = explorer.project_files(root)
            match = next(
                (
                    entry
                    for entry in entries
                    if wanted in {entry["path"], entry["name"]}
                ),
                None,
            ) or next(
                (entry for entry in entries if entry["name"] == name),
                None,
            )
            if match is not None:
                file_path = root / str(match["path"])
                if file_path.is_file():
                    text = _read_text(file_path)
                    return {
                        "file": {
                            "name": match["name"],
                            "path": match["path"],
                            "kind": match["kind"],
                            "size": int(match["size"]),
                        },
                        "content": text,
                        "binary": not text
                        and file_path.suffix.lower() not in TEXT_SUFFIXES,
                        "truncated": "…（已截断）" in text,
                    }
        items = research.list_items(project_id)
        item = next(
            (
                row
                for row in items
                if wanted in {row.get("path"), row.get("title")}
            ),
            None,
        )
        if item is None:
            name = safe_name(wanted)
            item = next(
                (
                    row
                    for row in items
                    if Path(str(row.get("path") or "")).name == name
                ),
                None,
            )
        if item is None:
            raise HTTPException(status_code=404, detail="file not found")
        file_path = research.item_path(project_id, item)
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        text = _read_text(file_path)
        return {
            "file": {
                "name": item["title"],
                "path": item.get("path") or "",
                "kind": item["kind"],
                "size": int(item.get("size") or 0),
            },
            "content": text,
            "binary": not text and file_path.suffix.lower() not in TEXT_SUFFIXES,
            "truncated": "…（已截断）" in text,
        }

    @router.get("/sessions/{session_id}/review/templates")
    def review_templates(
        session_id: str,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        """可选综述模板：系统默认 / 内置 / 应用设置 / 项目内上传。"""
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        project_id = str(project["id"])
        files_dir = Path(research.workspace_root(project_id))
        global_root = _global_template_root(research)
        project_choice = _read_review_template(research, project_id)
        global_choice = template_store.read_choice(global_root)
        selected = project_choice or global_choice or "default"

        items = list_templates(files_dir)
        known = {str(item.get("id")) for item in items}
        for item in template_store.list_options(global_root):
            if item["id"] not in known:
                items.append(item)
                known.add(str(item["id"]))
        detail = template_store.describe(selected, global_root, library)
        guide = detail.get("guide") or {}
        if selected.startswith("paper:") and detail.get("name"):
            items.append(
                {
                    "id": selected,
                    "name": detail["name"],
                    "source": "paper",
                    "description": "来自文献库的综述结构",
                    "sections": detail.get("sections") or [],
                    "guide": guide,
                }
            )
        return {
            "selected": selected,
            "scope": "project" if project_choice else "global" if global_choice else "default",
            "globalSelected": global_choice or "default",
            "guide": guide,
            "templates": items,
        }

    @router.get("/sessions/{session_id}/citations/check")
    def check_citations(
        session_id: str,
        source: str | None = Query(default=None, description="草稿文件名或产物标题"),
        template: str | None = Query(default=None, description="模板 id 或项目内模板文件名"),
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        """审计综述草稿的引用结构，返回可验证的页码/来源指标。"""
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        project_id = str(project["id"])
        title, text = _export_source(research, project_id, source)
        if not text:
            raise HTTPException(status_code=404, detail="没有找到可审计的 Markdown 草稿")
        report = audit_review(text, project_id, library)
        report["source"] = {"title": title or source or "未命名", "chars": len(text)}
        # 草稿与“标准综述”的差距：让 agent 每轮都能看到还缺哪些章节、证据与引用。
        choice = _effective_template_choice(research, project_id, template)
        sections, label, error = _resolve_review_template(
            choice,
            Path(research.workspace_root(project_id)),
            research,
            library,
        )
        template_kind = BUILTIN_TEMPLATES.get(choice, {}).get("kind", "review")
        report["standard"] = evaluate_review(
            text,
            report,
            sections=sections,
            template_label=label,
            kind=template_kind,
        )
        if error:
            report["standard"]["error"] = error
        return report

    @router.post("/sessions/{session_id}/run")
    def run_terminal(
        session_id: str,
        body: RunIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        """在项目 files/ 目录内执行一条命令。"""
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        try:
            return run_command(
                research,
                str(project["id"]),
                body.command,
                cwd=body.cwd,
                timeout_ms=body.timeout_ms,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        template = patch.pop("review_template", None)
        if template is not None:
            _write_review_template(research, str(project["id"]), str(template))
        if not patch:
            return {"project": _project_payload(project, research, library)}
        updated = research.update_project(str(project["id"]), **patch)
        if updated is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {"project": _project_payload(updated, research, library)}

    # ── 项目文件 / 产物 ────────────────────────────────

    @router.post("/sessions/{session_id}/files")
    def write_file(
        session_id: str,
        body: FileIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        workspace = Path(research.workspace_root(str(project["id"])))
        fallback = (body.title or "未命名").strip() + ".md"
        filename = safe_name(body.filename or fallback)
        relative, size = save_bytes_in(workspace, filename, body.content.encode("utf-8"))
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

    def _docx_target(
        project: dict[str, Any], wanted: str
    ) -> tuple[str, dict[str, Any], Path]:
        """把路径 / 标题 / 文件名定位到项目里的 .docx 文件。"""
        project_id = str(project["id"])
        target = wanted.strip()
        root = Path(research.workspace_root(project_id))
        if str(project.get("root_path") or "").strip():
            try:
                direct = explorer.project_file(root, target)
            except (FileNotFoundError, ValueError):
                direct = None
            if direct is not None and direct.suffix.lower() == ".docx":
                relative = direct.relative_to(root).as_posix()
                return (
                    project_id,
                    {"id": "", "path": relative, "title": direct.name},
                    direct,
                )
        items = research.list_items(project_id)
        item = next(
            (
                row
                for row in items
                if target in {str(row.get("path") or ""), str(row.get("title") or "")}
            ),
            None,
        )
        if item is None:
            name = safe_name(target)
            item = next(
                (
                    row
                    for row in items
                    if Path(str(row.get("path") or "")).name == name
                ),
                None,
            )
        if item is None:
            raise HTTPException(status_code=404, detail="file not found")
        file_path = research.item_path(project_id, item)
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        if file_path.suffix.lower() != ".docx":
            raise HTTPException(status_code=400, detail="只支持 .docx 文件")
        return project_id, item, file_path

    @router.get("/sessions/{session_id}/docx")
    def docx_read_file(
        session_id: str,
        path: str = Query(..., description="项目里的 .docx 路径或文件名"),
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        """读取 docx 的段落 / 表格结构，供 agent 定位要修改的地方。"""
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        _project_id, item, file_path = _docx_target(project, path)
        try:
            result = read_docx(file_path)
        except DocxError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        result["file"].update(
            {"path": item.get("path") or "", "itemId": item.get("id") or None}
        )
        return result

    @router.post("/sessions/{session_id}/docx")
    def docx_edit_file(
        session_id: str,
        body: DocxEditIn,
        x_internal_token: str | None = Header(default=None, alias="x-internal-token"),
    ) -> dict[str, Any]:
        """按操作列表就地修改项目里的 docx，保存后同步文件列表里的大小。"""
        _check(x_internal_token)
        _session, project = _session_project(session_id)
        project_id, item, file_path = _docx_target(project, body.path)
        try:
            result = edit_docx(
                file_path,
                body.operations,
                Path(research.workspace_root(project_id)),
            )
        except DocxError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if item.get("id"):
            research.set_item_size(
                project_id, str(item["id"]), result["file"]["size"]
            )
        result["file"].update(
            {"path": item.get("path") or "", "itemId": item.get("id") or None}
        )
        return result

    def _write_artifact(
        project: dict[str, Any], session_id: str, body: ArtifactIn
    ) -> dict[str, Any]:
        project_id = str(project["id"])
        target_dir = research.artifacts_root(project_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        title = (body.title or "").strip()
        fallback = (title or "未命名产物") + ".md"

        # 同名产物视为“同一份稿子的新版本”：覆盖文件、版本号 +1，
        # 而不是生成 综述初稿-1.md / 综述初稿-2.md 这样的一串副本。
        existing = None
        for row in research.list_artifacts(project_id):
            row_title = (row.get("title") or "").strip()
            if title and row_title == title:
                existing = row
                break
            if (
                not title
                and body.filename
                and Path(str(row.get("path") or "")).name == safe_name(body.filename)
            ):
                existing = row
                break

        if existing is not None:
            filename = Path(str(existing.get("path") or "")).name or safe_name(fallback)
            target = target_dir / filename
            target.write_text(body.content, encoding="utf-8")
            row = research.revise_artifact(
                str(existing["id"]),
                path=filename,
                session_id=session_id,
                kind=body.kind or existing.get("kind"),
            )
            return _artifact_payload(row)

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
            project_id,
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
        project_id = str(row["project_id"])
        path = research.artifacts_root(project_id) / str(row.get("path") or "")
        if not path.is_file():
            path = research.legacy_artifacts_root(project_id) / str(row.get("path") or "")
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
        root = Path(research.workspace_root(pid))
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
            relative, size = save_bytes_in(root, filename, text.encode("utf-8"))
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
            label = body.source or body.title or "综述草稿"
            return _export_latex(
                research,
                project,
                body,
                fmt,
                audit=lambda text: _audit_export(
                    research, library, project, text, label, session_id
                ),
                library=library,
            )

        if fmt in {"pptx", "ppt"}:
            return _export_pptx(research, project, body)

        if fmt != "docx":
            return {
                "format": fmt,
                "files": [],
                "error": "只支持 docx、bib、latex、pdf、pptx 五种格式。",
            }

        title, text = _export_source(research, pid, body.source)
        if body.content:
            text = body.content
        if not text.strip():
            return {
                "format": "docx",
                "files": [],
                "error": "没有找到要导出的正文：请传 content，或 source 指向项目里的 markdown 文件。",
            }
        citation = _audit_export(
            research,
            library,
            project,
            text,
            body.source or title or "综述草稿",
            session_id,
        )
        exe = find_pandoc()
        if not exe:
            from server.toolchain_api import invalidate_cache

            invalidate_cache()
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
        relative, size = save_bytes_in(root, filename, data)
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
            "citation": citation,
        }

    return router
