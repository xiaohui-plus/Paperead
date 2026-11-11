"""研究功能 REST API（项目 / 文件树 / 会话索引 / 会话事件）。

产品记录落 SQLite；对话与流式事件转发给本地 Node agent，
事件同时按 Python 自增 seq 落库，刷新页面后可以完整重放。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from server.research import agent_client, explorer, templates as review_templates
from server.research.files import safe_name, save_bytes_in
from server.research.store import ResearchStore

MAX_UPLOAD = 200 * 1024 * 1024
FOLDER_MAX_FILES = 1000
FOLDER_MAX_BYTES = 500 * 1024 * 1024
OFFLINE_NOTICE = {
    "type": "notice",
    "level": "warn",
    "code": "agent.offline",
    "text": "研究 agent 尚未启动，只能查看历史消息。",
}


class ProjectCreateIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: str
    name: str | None = None
    description: str = ""
    root_path: str | None = Field(default=None, alias="rootPath")


class ProjectPatchIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    description: str | None = None
    archived: bool | None = None
    root_path: str | None = Field(default=None, alias="rootPath")


class ProjectRootIn(BaseModel):
    path: str = ""


class ReviewTemplateSelectIn(BaseModel):
    id: str


class FolderCreateIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    parent_id: str | None = Field(default=None, alias="parentId")


class ItemPatchIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = None
    parent_id: str | None = Field(default=None, alias="parentId")


class ItemCopyIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    parent_id: str | None = Field(default=None, alias="parentId")


class SessionCreateIn(BaseModel):
    title: str | None = None


class MessageRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: str = "file"
    name: str = ""
    path: str = ""
    paper_id: str | None = Field(default=None, alias="paperId")


class MessageIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    text: str
    refs: list[MessageRef] = Field(default_factory=list)
    mode: str = "standard"
    scope: str = "workspace"


class CommandIn(BaseModel):
    line: str


def _sse(event: dict[str, Any]) -> str:
    body = json.dumps(event, ensure_ascii=False)
    return f"id: {event['seq']}\ndata: {body}\n\n"


# 一次轮次里可能长时间没有事件（例如跑 120s 的终端命令），空闲上限要留足。
PUMP_IDLE_SECONDS = 900.0
# turn.end 之后再收几秒，避免漏掉收尾的标题等事件。
PUMP_TAIL_SECONDS = 3.0


class _AgentEventPump:
    """把 agent 事件持续落库，不依赖任何 SSE 连接是否还在。

    之前只有 ``/events`` 的 SSE 在订阅时才写库：页面关掉、切走、白屏或断网，
    agent 的回复就只留在内存里，刷新后只剩下用户自己的消息。改成独立后台
    任务后，页面在不在线都能保存。
    """

    def __init__(self, store: ResearchStore) -> None:
        self._store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._errors: dict[str, str] = {}

    def running(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    def error(self, session_id: str) -> str | None:
        return self._errors.get(session_id)

    async def start(self, session_id: str, workspace: str) -> None:
        if self.running(session_id):
            return
        self._errors.pop(session_id, None)
        self._tasks[session_id] = asyncio.create_task(
            self._run(session_id, workspace)
        )

    async def restart(self, session_id: str, workspace: str) -> None:
        """新一轮消息：取消旧任务，从 agent 历史重新订阅，避免漏掉开头的事件。"""
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.start(session_id, workspace)

    async def _run(self, session_id: str, workspace: str) -> None:
        base = self._store.latest_seq(session_id)
        stream = agent_client.stream_events(session_id, 0)
        iterator = stream.__aiter__()
        deadline: float | None = None
        try:
            await agent_client.ensure_session(session_id, workspace)
            while True:
                timeout = PUMP_IDLE_SECONDS
                if deadline is not None:
                    timeout = deadline - asyncio.get_running_loop().time()
                    if timeout <= 0:
                        break
                try:
                    raw = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    # 还没等到本轮 turn.end 就断流，说明 agent 侧异常退出，
                    # 不能静默停泵让会话永远停在 running。
                    if deadline is None:
                        self._errors[session_id] = (
                            "研究 agent 事件流提前结束，本轮没有收到结束事件"
                        )
                    break
                except asyncio.TimeoutError:
                    # 这里必须退出：wait_for 取消的是异步生成器，继续读会立刻结束。
                    break
                agent_seq = int(raw.get("seq") or 0)
                event = (
                    self._store.append_agent_event(session_id, agent_seq, raw)
                    if agent_seq
                    else self._store.append_event(session_id, raw)
                )
                if event["seq"] <= base:
                    continue
                if event.get("type") == "turn.end":
                    deadline = (
                        asyncio.get_running_loop().time() + PUMP_TAIL_SECONDS
                    )
        except asyncio.CancelledError:
            raise
        except agent_client.AgentUnavailable as error:
            self._errors[session_id] = str(error)
        except Exception as error:  # noqa: BLE001 - 后台任务不能把异常抛给事件循环
            self._errors[session_id] = str(error)
        finally:
            if self._tasks.get(session_id) is asyncio.current_task():
                self._tasks.pop(session_id, None)
            with contextlib.suppress(Exception):
                await stream.aclose()


def _project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "description": row.get("description") or "",
        "archived": bool(row.get("archived")),
        "rootPath": row.get("root_path") or "",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "sessionCount": int(row.get("session_count") or 0),
        "running": bool(row.get("running_count") or 0),
    }


def _item(row: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(row.get("meta") or "{}")
    except Exception:
        meta = {}
    return {
        "id": row["id"],
        "parentId": row.get("parent_id"),
        "kind": row["kind"],
        "name": row["title"],
        "path": row.get("path") or "",
        "mime": row.get("mime") or "",
        "size": int(row.get("size") or 0),
        "refId": row.get("ref_id"),
        "meta": meta if isinstance(meta, dict) else {},
        "addedAt": row["added_at"],
    }


def _artifact(row: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(row.get("meta") or "{}")
    except Exception:
        meta = {}
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "sessionId": row.get("session_id"),
        "kind": row.get("kind") or "",
        "title": row.get("title") or "",
        "path": row.get("path") or "",
        "version": int(row.get("version") or 1),
        "createdAt": row["created_at"],
        "meta": meta if isinstance(meta, dict) else {},
    }


def _session(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "title": row["title"],
        "status": row["status"],
        "model": row["model"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "lastTurnAt": row.get("last_turn_at"),
        "running": row["status"] == "running",
    }


def _subtree_paper_ids(
    store: ResearchStore, project_id: str, item_id: str
) -> list[str]:
    """删除文件夹时，收集子树里所有“项目论文”的 refId，便于一并清理。"""
    children: dict[str | None, list[dict[str, Any]]] = {}
    for row in store.list_items(project_id):
        children.setdefault(row.get("parent_id"), []).append(row)
    papers: list[str] = []
    root = store.get_item(item_id)
    if root is not None and root.get("kind") == "paper" and root.get("ref_id"):
        papers.append(str(root["ref_id"]))
    stack = [item_id]
    while stack:
        current = stack.pop()
        for row in children.get(current, []):
            stack.append(row["id"])
            if row.get("kind") == "paper" and row.get("ref_id"):
                papers.append(str(row["ref_id"]))
    return papers


def build_router(
    store: ResearchStore,
    library: Any | None = None,
    parse_paper: Any | None = None,
    data_dir: Path | None = None,
) -> APIRouter:
    """研究项目 API。

    ``library`` 用于两件事：项目论文的 PDF 从文献库读取；右键“加入文献库”。
    ``parse_paper`` 是“入库后后台解析”的回调（由 main 注入）。
    ``data_dir`` 用来定位应用级设置（综述模板），缺省时从 ``research`` 目录回退。
    """
    router = APIRouter(prefix="/api/research", tags=["research"])
    pump = _AgentEventPump(store)

    def reconcile_sessions() -> None:
        """进程重启后把卡在 running 的会话标回 idle，避免一直显示在运行。"""
        for session in store.list_all_sessions():
            if session.get("status") == "running":
                store.update_session(str(session["id"]), status="idle")

    router.add_event_handler("startup", reconcile_sessions)

    def _local_only(request: Request) -> None:
        """文件夹浏览只对本机窗口开放，避免局域网设备读取电脑文件。"""
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail="只能在桌面本机浏览文件夹")

    # ── 综述模板（应用级设置）────────────────────────────

    review_hint = re.compile(r"survey|review|综述|overview|tutorial", re.IGNORECASE)

    def review_root() -> Path:
        if data_dir is not None:
            return review_templates.templates_root(data_dir)
        return review_templates.root_for(Path(store.root))

    def library_candidates() -> list[dict[str, Any]]:
        if library is None:
            return []
        try:
            rows = library.list()
        except Exception:
            return []
        items: list[dict[str, Any]] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "id": row["id"],
                    "title": title,
                    "authors": str(row.get("authors") or ""),
                    "year": row.get("year") or "",
                    "pages": int(row.get("pages") or 0),
                    "status": str(row.get("status") or ""),
                    "isReview": bool(review_hint.search(title)),
                }
            )
        return items

    def templates_payload() -> dict[str, Any]:
        root = review_root()
        selected = review_templates.read_choice(root) or "default"
        items = review_templates.list_options(root)
        detail = review_templates.describe(selected, root, library)
        if selected.startswith("paper:") and detail.get("name"):
            items.append(
                {
                    "id": selected,
                    "name": detail["name"],
                    "source": "paper",
                    "description": "来自文献库的综述结构",
                    "sections": detail.get("sections") or [],
                }
            )
        return {
            "selected": selected,
            "templates": items,
            "selectedDetail": detail,
            "library": library_candidates(),
        }

    def ensure_choice(root: Path, value: str) -> str:
        value = (value or "").strip() or "default"
        if value in {"default", "auto"} or value in review_templates.BUILTIN_TEMPLATES:
            return value
        if value.startswith("upload:"):
            path = root / review_templates.UPLOADS_DIR / safe_name(value[7:])
            if path.is_file():
                return value
            raise HTTPException(status_code=404, detail="模板文件不存在")
        if value.startswith("paper:"):
            paper_id = value[6:]
            if library is not None and library.get(paper_id) is not None:
                return value
            raise HTTPException(status_code=404, detail="文献库里没有这篇论文")
        raise HTTPException(status_code=400, detail="不认识的模板 id")

    @router.get("/review-templates")
    def get_review_templates() -> dict[str, Any]:
        """设置页：系统默认 / 内置 / 已上传 / 文献库候选与当前选择。"""
        return templates_payload()

    @router.post("/review-templates/select")
    def select_review_template(body: ReviewTemplateSelectIn) -> dict[str, Any]:
        root = review_root()
        value = ensure_choice(root, body.id)
        review_templates.write_choice(root, value)
        return templates_payload()

    @router.post("/review-templates/upload")
    async def upload_review_template(file: UploadFile = File(...)) -> dict[str, Any]:
        root = review_root()
        data = await file.read()
        try:
            item = review_templates.save_upload(root, file.filename or "", data)
        except review_templates.TemplateError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        review_templates.write_choice(root, str(item["id"]))
        payload = templates_payload()
        payload["uploaded"] = item
        return payload

    @router.delete("/review-templates/uploads/{name}")
    def delete_review_template(name: str) -> dict[str, Any]:
        if not review_templates.delete_upload(review_root(), name):
            raise HTTPException(status_code=404, detail="模板文件不存在")
        return templates_payload()

    @router.get("/review-templates/paper/{paper_id}")
    def preview_review_template(paper_id: str, refresh: bool = Query(default=False)) -> dict[str, Any]:
        """预览文献库论文的章节骨架（首次会解析 PDF，之后读缓存）。"""
        root = review_root()
        sections, name, error = review_templates.paper_template(
            root, library, paper_id, refresh=refresh
        )
        return {
            "id": f"paper:{paper_id}",
            "name": name,
            "source": "paper",
            "sections": sections,
            "guide": review_templates.paper_guide(
                root, library, paper_id, refresh=refresh
            ),
            "error": error,
        }


    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "projects": len(store.list_projects())}

    @router.get("/projects")
    def list_projects(
        kind: str | None = Query(default=None),
        include_archived: bool = Query(default=False, alias="includeArchived"),
    ) -> dict[str, Any]:
        try:
            rows = store.list_projects(kind, include_archived)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"items": [_project(row) for row in rows]}

    @router.post("/projects", status_code=201)
    def create_project(body: ProjectCreateIn) -> dict[str, Any]:
        try:
            row = store.create_project(
                body.kind, body.name, body.description, body.root_path
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _project(row)

    @router.get("/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        row = store.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _project(row)

    @router.patch("/projects/{project_id}")
    def update_project(project_id: str, body: ProjectPatchIn) -> dict[str, Any]:
        patch = body.model_dump(exclude_none=True)
        try:
            row = store.update_project(project_id, **patch)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _project(row)

    @router.patch("/projects/{project_id}/root")
    def set_project_root(project_id: str, body: ProjectRootIn) -> dict[str, Any]:
        """把项目工作文件夹切换到本机任意文件夹；空路径恢复项目默认文件夹。"""
        try:
            row = store.update_project(project_id, root_path=body.path)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _project(row)

    @router.get("/fs/roots")
    def fs_roots(request: Request) -> dict[str, Any]:
        """盘符与常用位置，供 Jupyter 式文件夹选择器使用。"""
        _local_only(request)
        return {"items": explorer.quick_roots()}

    @router.get("/fs/list")
    def fs_list(
        request: Request, path: str = Query(default="")
    ) -> dict[str, Any]:
        """列出本机某个文件夹；path 为空时返回盘符/常用位置。"""
        _local_only(request)
        try:
            return explorer.browse(path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except NotADirectoryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.delete("/projects/{project_id}")
    def archive_project(project_id: str) -> dict[str, Any]:
        row = store.update_project(project_id, archived=True)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _project(row)

    @router.delete("/projects/{project_id}/purge")
    async def purge_project(project_id: str) -> dict[str, Any]:
        """永久删除项目及其全部文件、会话和产物，无法恢复。"""
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        # 先让 agent 停掉这个项目的轮次，避免删除后还在往已删目录里写
        for session in store.list_sessions(project_id):
            with contextlib.suppress(Exception):
                await agent_client.cancel(str(session["id"]))
        # 项目范围内入库的论文也要一起删；全局文献库条目不受影响。
        if library is not None:
            with contextlib.suppress(Exception):
                for paper in library.list_project(project_id):
                    library.remove(paper["id"])
        store.delete_project(project_id)
        return {"ok": True}

    @router.get("/projects/{project_id}/files")
    def list_files(project_id: str) -> dict[str, Any]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {"items": [_item(row) for row in store.list_items(project_id)]}

    @router.get("/projects/{project_id}/explorer")
    def project_explorer(
        request: Request,
        project_id: str,
        path: str = Query(default=""),
    ) -> dict[str, Any]:
        """按真实目录浏览项目工作文件夹，供 Jupyter 式文件面板使用。"""
        _local_only(request)
        try:
            root = store.workspace_root(project_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        try:
            return explorer.list_project(root, path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except NotADirectoryError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/projects/{project_id}/explorer/file")
    def project_explorer_file(
        request: Request,
        project_id: str,
        path: str = Query(...),
    ):
        """读取项目工作文件夹里的文件（只允许文件夹内相对路径）。"""
        _local_only(request)
        try:
            root = store.workspace_root(project_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        try:
            file_path = explorer.project_file(root, path)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=404, detail="file not found") from error
        media = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        return FileResponse(str(file_path), media_type=media)

    @router.post("/projects/{project_id}/folders", status_code=201)
    def create_folder(project_id: str, body: FolderCreateIn) -> dict[str, Any]:
        try:
            row = store.create_folder(project_id, body.name, body.parent_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _item(row)

    @router.post("/projects/{project_id}/files", status_code=201)
    async def upload_file(
        project_id: str,
        file: UploadFile = File(...),
        parent_id: str | None = Form(default=None, alias="parentId"),
    ) -> dict[str, Any]:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if project.get("archived"):
            raise HTTPException(status_code=409, detail="project archived")

        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty file")
        if len(data) > MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="file too large")

        parent_path = ""
        if parent_id:
            parent = store.get_item(parent_id)
            if parent is None or parent["project_id"] != project_id:
                raise HTTPException(status_code=404, detail="parent folder not found")
            if parent["kind"] != "folder":
                raise HTTPException(status_code=400, detail="parent is not a folder")
            parent_path = parent["path"]

        name = safe_name(file.filename or "未命名文件")
        try:
            relative, size = save_bytes_in(
                store.workspace_root(project_id), name, data, parent_path
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        mime = file.content_type or ""
        kind = "paper" if mime == "application/pdf" or name.lower().endswith(".pdf") else "file"
        row = store.add_file(
            project_id,
            name,
            relative,
            parent_id=parent_id,
            mime=mime,
            size=size,
            kind=kind,
        )
        return _item(row)

    @router.post("/projects/{project_id}/folder", status_code=201)
    async def upload_folder(
        project_id: str,
        files: list[UploadFile] = File(...),
        paths: str = Form(...),
        parent_id: str | None = Form(default=None, alias="parentId"),
    ) -> dict[str, Any]:
        """导入用户自己的文件夹：保留子目录结构，逐个落盘。"""
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if project.get("archived"):
            raise HTTPException(status_code=409, detail="project archived")
        try:
            raw_paths = json.loads(paths)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid paths") from error
        if not isinstance(raw_paths, list) or len(raw_paths) != len(files):
            raise HTTPException(status_code=400, detail="paths 与文件数量不一致")
        if len(files) > FOLDER_MAX_FILES:
            raise HTTPException(status_code=413, detail="文件夹里的文件太多")

        base_id = None
        base_path = ""
        if parent_id:
            parent = store.get_item(parent_id)
            if parent is None or parent["project_id"] != project_id:
                raise HTTPException(status_code=404, detail="parent folder not found")
            if parent["kind"] != "folder":
                raise HTTPException(status_code=400, detail="parent is not a folder")
            base_id = parent_id
            base_path = str(parent["path"] or "")

        items = store.list_items(project_id)
        folders = {
            (row.get("parent_id"), row["title"]): row
            for row in items
            if row["kind"] == "folder"
        }

        def ensure_folder(
            parent: str | None, parent_path: str, name: str
        ) -> dict[str, Any]:
            key = (parent, name)
            existing = folders.get(key)
            if existing:
                return existing
            relative = f"{parent_path}/{name}".strip("/")
            row = store.add_item(
                project_id, "folder", name, parent_id=parent, path=relative
            )
            items.append(row)
            folders[key] = row
            return row

        workspace = store.workspace_root(project_id)
        added: list[dict[str, Any]] = []
        errors: list[str] = []
        total = 0
        for upload, raw_path in zip(files, raw_paths):
            parts: list[str] = []
            for segment in str(raw_path or "").replace("\\", "/").split("/"):
                segment = segment.strip()
                if segment in ("", ".", "..", "__MACOSX"):
                    continue
                if segment in (".DS_Store", "Thumbs.db", "desktop.ini"):
                    continue
                cleaned = safe_name(segment, "")
                if cleaned:
                    parts.append(cleaned)
            if not parts:
                continue
            data = await upload.read()
            if not data:
                continue
            if len(data) > MAX_UPLOAD:
                errors.append(f"{parts[-1]}: 文件过大")
                continue
            total += len(data)
            if total > FOLDER_MAX_BYTES or len(added) >= FOLDER_MAX_FILES:
                errors.append("文件夹过大，已停止导入")
                break
            parent = base_id
            parent_path = base_path
            for name in parts[:-1]:
                folder = ensure_folder(parent, parent_path, name)
                parent = folder["id"]
                parent_path = str(folder["path"] or "")
            name = parts[-1]
            try:
                relative, size = save_bytes_in(workspace, name, data, parent_path)
                actual = Path(relative).name
                mime = mimetypes.guess_type(actual)[0] or (
                    upload.content_type or ""
                )
                kind = "paper" if actual.lower().endswith(".pdf") else "file"
                row = store.add_file(
                    project_id,
                    actual,
                    relative,
                    parent_id=parent,
                    mime=mime,
                    size=size,
                    kind=kind,
                )
                added.append(row)
            except Exception as error:  # noqa: BLE001 - 单个文件失败不影响整批
                errors.append(f"{name}: {error}"[:160])

        return {
            "items": [_item(row) for row in store.list_items(project_id)],
            "added": len(added),
            "errors": errors[:10],
        }

    @router.get("/projects/{project_id}/files/{item_id}/content")
    def file_content(project_id: str, item_id: str):
        item = store.get_item(item_id)
        if item is None or item["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="file not found")
        try:
            path = store.item_path(project_id, item)
        except (LookupError, ValueError) as error:
            raise HTTPException(status_code=404, detail="file not found") from error
        if not path.is_file():
            # 项目内的“论文”条目没有独立文件：PDF 存在文献库的 papers 目录。
            ref_id = str(item.get("ref_id") or "")
            if item["kind"] == "paper" and ref_id and library is not None:
                pdf = library.pdf_path(ref_id)
                if pdf is not None and pdf.is_file():
                    return FileResponse(str(pdf), media_type="application/pdf")
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(str(path), media_type=item.get("mime") or None)

    @router.patch("/projects/{project_id}/items/{item_id}")
    def update_item(
        project_id: str, item_id: str, body: ItemPatchIn
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if body.name is not None:
            patch["name"] = body.name
        if "parent_id" in body.model_fields_set:
            patch["parent_id"] = body.parent_id
        if not patch:
            raise HTTPException(status_code=400, detail="nothing to update")
        try:
            items = store.update_item(project_id, item_id, **patch)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"items": [_item(row) for row in items]}

    @router.post("/projects/{project_id}/items/{item_id}/copy", status_code=201)
    def copy_item(
        project_id: str, item_id: str, body: ItemCopyIn
    ) -> dict[str, Any]:
        try:
            items = store.copy_item(project_id, item_id, body.parent_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"items": [_item(row) for row in items]}

    @router.delete("/projects/{project_id}/items/{item_id}")
    def delete_item(project_id: str, item_id: str) -> dict[str, Any]:
        item = store.get_item(item_id)
        if item is None or item["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="item not found")
        paper_ids = _subtree_paper_ids(store, project_id, item_id)
        store.remove_item(item_id)
        remaining = store.list_items(project_id)
        # 复制的论文条目共用同一个 refId，只有最后一条被删时才清理文献库。
        still_used = {
            str(row.get("ref_id") or "")
            for row in remaining
            if row.get("kind") == "paper"
        }
        if library is not None:
            for paper_id in paper_ids:
                if paper_id in still_used:
                    continue
                row = library.get(paper_id)
                # 只删除属于这个项目的论文；已在全局文献库里的不动。
                if row is not None and row.get("project_id") == project_id:
                    library.remove(paper_id)
        return {"items": [_item(row) for row in remaining]}

    @router.post("/projects/{project_id}/items/{item_id}/to-library")
    def add_to_library(project_id: str, item_id: str) -> dict[str, Any]:
        """右键“加入文献库”：只接受 PDF（论文条目或上传的 .pdf 文件）。"""
        if library is None:
            raise HTTPException(status_code=503, detail="文献库不可用")
        item = store.get_item(item_id)
        if item is None or item["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="item not found")

        ref_id = str(item.get("ref_id") or "")
        if item["kind"] == "paper" and ref_id:
            row = library.get(ref_id)
            if row is None:
                raise HTTPException(status_code=404, detail="论文记录不存在")
            if (row.get("project_id") or None) != project_id:
                return {"ok": True, "already": True, "paperId": ref_id}
            existing = library.find_by_source(
                str(row.get("source_id") or ""), project_id=None
            )
            if existing is not None:
                return {"ok": True, "already": True, "paperId": existing["id"]}
            library.update(ref_id, project_id="")
            return {"ok": True, "already": False, "paperId": ref_id}

        if item["kind"] in {"file", "paper"} and str(
            item.get("path") or item.get("name") or ""
        ).lower().endswith(".pdf"):
            try:
                path = store.item_path(project_id, item)
            except (LookupError, ValueError) as error:
                raise HTTPException(status_code=404, detail="文件不存在") from error
            if not path.is_file():
                raise HTTPException(status_code=404, detail="文件不存在")
            row = library.add(
                path.name, path.read_bytes(), {"title": item["title"], "source": "upload"}
            )
            if parse_paper is not None:
                parse_paper(row["id"])
            return {"ok": True, "already": False, "paperId": row["id"]}

        raise HTTPException(status_code=400, detail="只有 PDF 文件可以加入文献库")

    @router.get("/projects/{project_id}/artifacts")
    def list_artifacts(project_id: str) -> dict[str, Any]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {
            "items": [
                _artifact(row)
                for row in store.list_artifacts(project_id)
            ]
        }

    @router.get("/projects/{project_id}/artifacts/{artifact_id}/content")
    def artifact_content(project_id: str, artifact_id: str):
        row = store.get_artifact(artifact_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="artifact not found")
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        path = Path(project["workspace"]) / "artifacts" / (row.get("path") or "")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact file not found")
        mime = mimetypes.guess_type(path.name)[0] or "text/markdown"
        return FileResponse(str(path), media_type=mime)

    @router.get("/projects/{project_id}/sessions")
    def list_sessions(project_id: str) -> dict[str, Any]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {"items": [_session(row) for row in store.list_sessions(project_id)]}

    @router.post("/projects/{project_id}/sessions", status_code=201)
    def create_session(project_id: str, body: SessionCreateIn | None = None) -> dict[str, Any]:
        try:
            row = store.create_session(project_id, (body.title if body else None))
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _session(row)

    # ── 会话消息 / 事件流 ──────────────────────────────

    def _session_or_404(session_id: str) -> dict[str, Any]:
        session = store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    def _workspace(session: dict[str, Any]) -> str:
        project = store.get_project(session["project_id"])
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return str(store.workspace_root(str(project["id"])))

    @router.get("/sessions/{session_id}/commands")
    async def list_session_commands(session_id: str) -> dict[str, Any]:
        session = _session_or_404(session_id)
        cwd = _workspace(session)
        try:
            await agent_client.ensure_session(session_id, cwd)
            items = await agent_client.list_commands(session_id)
        except agent_client.AgentUnavailable as error:
            raise HTTPException(status_code=503, detail="AGENT_UNAVAILABLE") from error
        return {"items": items}

    @router.post("/sessions/{session_id}/commands", status_code=202)
    async def run_session_command(
        session_id: str, body: CommandIn
    ) -> dict[str, Any]:
        _session_or_404(session_id)
        line = body.line.strip()
        if not line.startswith("/"):
            raise HTTPException(status_code=400, detail="命令必须以 / 开头")
        cwd = _workspace(_session_or_404(session_id))
        try:
            await agent_client.ensure_session(session_id, cwd)
            result = await agent_client.run_command(session_id, line)
        except agent_client.AgentUnavailable as error:
            raise HTTPException(status_code=503, detail="AGENT_UNAVAILABLE") from error
        event = store.append_event(
            session_id,
            {
                "type": "command.result",
                "name": result.get("name") or "",
                "kind": result.get("kind") or "success",
                "text": result.get("text") or "",
                "line": line,
            },
        )
        return {"ok": True, "event": event}

    @router.post("/sessions/{session_id}/messages", status_code=202)
    async def post_message(session_id: str, body: MessageIn) -> dict[str, Any]:
        session = _session_or_404(session_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="消息不能为空")

        refs = [
            item.model_dump(exclude_none=True)
            for item in body.refs
            if item.path or item.paper_id
        ]
        agent_text = text
        if refs:
            lines: list[str] = []
            for item in refs:
                name = item.get("name") or item.get("path") or ""
                suffix = (
                    f"（论文 {item['paper_id']}）"
                    if item.get("paper_id")
                    else f"（路径：{item.get('path')}）"
                )
                lines.append(f"- {name}{suffix}")
            agent_text += "\n\n引用对象（用户指定，优先读取）：\n" + "\n".join(lines)
        if body.mode == "deep":
            agent_text += (
                "\n\n[研究方式] 深度研究：先做多轮检索与交叉核验，"
                "列明证据来源，不要跳步。"
            )
        if body.scope == "readonly":
            agent_text += (
                "\n\n[工作区权限] 只读：只分析与回答，"
                "不要修改、新建或删除工作区文件。"
            )

        # 先落用户消息：即使 agent 掉线，刷新后也能看到自己发过什么。
        store.append_event(
            session_id,
            {
                "type": "message.user",
                "text": text,
                "refs": refs,
                "mode": body.mode,
                "scope": body.scope,
            },
        )

        cwd = _workspace(session)
        try:
            await agent_client.ensure_session(session_id, cwd)
            # 订阅先于发送建立：即使页面立刻关掉，后台任务也会把回复落库。
            # shield 避免浏览器中断 POST 时把 restart 一起取消，导致这一轮没有后台泵。
            await asyncio.shield(pump.restart(session_id, cwd))
            await agent_client.send_message(session_id, agent_text, cwd)
        except agent_client.AgentUnavailable as error:
            store.append_event(
                session_id,
                {
                    "type": "notice",
                    "level": "error",
                    "code": "agent.offline",
                    "text": str(error),
                },
            )
            raise HTTPException(status_code=503, detail="AGENT_UNAVAILABLE") from error
        return {"ok": True}

    @router.post("/sessions/{session_id}/cancel", status_code=202)
    async def cancel_session(session_id: str) -> dict[str, Any]:
        _session_or_404(session_id)
        try:
            await agent_client.cancel(session_id)
        except agent_client.AgentUnavailable as error:
            raise HTTPException(status_code=503, detail="AGENT_UNAVAILABLE") from error
        return {"ok": True}

    @router.get("/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        session = _session_or_404(session_id)
        workspace = _workspace(session)

        async def replay_notice(text: str, level: str = "warn") -> str:
            # 离线提示只在本次连接内下发，不落库：否则每次刷新都会多一条。
            return _sse(
                {
                    "seq": 0,
                    "at": int(time.time() * 1000),
                    "type": "notice",
                    "level": level,
                    "code": "agent.offline",
                    "text": text,
                }
            )

        async def relay() -> Any:
            cursor = after
            for event in store.list_events(session_id, cursor):
                cursor = event["seq"]
                yield _sse(event)

            if not agent_client.available():
                yield await replay_notice(OFFLINE_NOTICE["text"])
                return

            try:
                await agent_client.ensure_session(session_id, workspace)
            except agent_client.AgentUnavailable as error:
                yield await replay_notice(str(error))
                return

            # 事件落库交给后台任务：页面关掉、断网或白屏都不会丢回复。
            # 这里只负责把库里的新事件推给当前页面，多个标签页也不会重复写。
            await pump.start(session_id, workspace)
            last_ping = time.monotonic()
            while True:
                await asyncio.sleep(0.35)
                for event in store.list_events(session_id, cursor):
                    cursor = event["seq"]
                    yield _sse(event)
                error = pump.error(session_id)
                if error and not pump.running(session_id):
                    yield await replay_notice(error, "error")
                    return
                if time.monotonic() - last_ping > 15:
                    last_ping = time.monotonic()
                    yield ": ping\n\n"

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache, no-transform",
                "x-accel-buffering": "no",
                "connection": "keep-alive",
            },
        )

    return router
