"""研究功能 REST API（项目 / 文件树 / 会话索引 / 会话事件）。

产品记录落 SQLite；对话与流式事件转发给本地 Node agent，
事件同时按 Python 自增 seq 落库，刷新页面后可以完整重放。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from server.research import agent_client
from server.research.files import safe_name, save_bytes
from server.research.store import ResearchStore

MAX_UPLOAD = 200 * 1024 * 1024
OFFLINE_NOTICE = {
    "type": "notice",
    "level": "warn",
    "code": "agent.offline",
    "text": "研究 agent 尚未启动，只能查看历史消息。",
}


class ProjectCreateIn(BaseModel):
    kind: str
    name: str | None = None
    description: str = ""


class ProjectPatchIn(BaseModel):
    name: str | None = None
    description: str | None = None
    archived: bool | None = None


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


class MessageIn(BaseModel):
    text: str


def _sse(event: dict[str, Any]) -> str:
    body = json.dumps(event, ensure_ascii=False)
    return f"id: {event['seq']}\ndata: {body}\n\n"


def _project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "description": row.get("description") or "",
        "archived": bool(row.get("archived")),
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
) -> APIRouter:
    """研究项目 API。

    ``library`` 用于两件事：项目论文的 PDF 从文献库读取；右键“加入文献库”。
    ``parse_paper`` 是“入库后后台解析”的回调（由 main 注入）。
    """
    router = APIRouter(prefix="/api/research", tags=["research"])

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
            row = store.create_project(body.kind, body.name, body.description)
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
        row = store.update_project(project_id, **patch)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _project(row)

    @router.delete("/projects/{project_id}")
    def archive_project(project_id: str) -> dict[str, Any]:
        row = store.update_project(project_id, archived=True)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _project(row)

    @router.get("/projects/{project_id}/files")
    def list_files(project_id: str) -> dict[str, Any]:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {"items": [_item(row) for row in store.list_items(project_id)]}

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
            relative, size = save_bytes(
                store.workspace(project_id), name, data, parent_path
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
        if library is not None:
            for paper_id in paper_ids:
                row = library.get(paper_id)
                # 只删除属于这个项目的论文；已在全局文献库里的不动。
                if row is not None and row.get("project_id") == project_id:
                    library.remove(paper_id)
        return {"items": [_item(row) for row in store.list_items(project_id)]}

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
        return str(project["workspace"])

    @router.post("/sessions/{session_id}/messages", status_code=202)
    async def post_message(session_id: str, body: MessageIn) -> dict[str, Any]:
        session = _session_or_404(session_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="消息不能为空")

        # 先落用户消息：即使 agent 掉线，刷新后也能看到自己发过什么。
        store.append_event(session_id, {"type": "message.user", "text": text})

        cwd = _workspace(session)
        try:
            await agent_client.ensure_session(session_id, cwd)
            await agent_client.send_message(session_id, text, cwd)
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

            # agent 的 in-memory 序号会在进程重启后回到 1，所以每次都从头订阅，
            # 由 (agent_seq, at) 去重；只有新事件才会得到新的 Python seq。
            queue: asyncio.Queue[Any] = asyncio.Queue()

            async def pump() -> None:
                try:
                    async for raw in agent_client.stream_events(session_id, 0):
                        await queue.put(raw)
                    await queue.put(None)
                except agent_client.AgentUnavailable as error:
                    await queue.put(error)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - 上游异常统一降级为提示
                    await queue.put(agent_client.AgentUnavailable(str(error)))

            task = asyncio.create_task(pump())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if item is None:
                        break
                    if isinstance(item, agent_client.AgentUnavailable):
                        yield await replay_notice(str(item), "error")
                        break
                    agent_seq = int(item.get("seq") or 0)
                    event = (
                        store.append_agent_event(session_id, agent_seq, item)
                        if agent_seq
                        else store.append_event(session_id, item)
                    )
                    if event["seq"] <= cursor:
                        continue
                    cursor = event["seq"]
                    yield _sse(event)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

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
