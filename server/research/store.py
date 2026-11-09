"""研究项目存储：项目、项目文件树、会话索引。

会话正文不在这里：DSH 日志才是真源，本表只存标题、状态与时间。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from server.research.db import connect, init_db
from server.research.files import remove_file, resolve_in_files, safe_name

# 区分“未传 parent_id”与“移动到根目录（None）”。
_UNSET = object()

PROJECT_KINDS = {"research", "review"}
ITEM_KINDS = {"paper", "file", "folder", "note"}
MAX_NAME = 80
MAX_TITLE = 180


def _now() -> int:
    return int(time.time())


def _clean(value: str | None, limit: int, fallback: str) -> str:
    text = (value or "").strip()
    return (text or fallback)[:limit]


class ResearchStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "research.db"
        self.projects_dir = self.root / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        init_db(self.db_path)

    # ── 连接 ────────────────────────────────────────────

    @contextmanager
    def _db(self) -> Iterator[Any]:
        conn = connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── 项目 ────────────────────────────────────────────

    def create_project(
        self,
        kind: str,
        name: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        kind = (kind or "research").strip()
        if kind not in PROJECT_KINDS:
            raise ValueError("invalid project kind")
        project_id = f"p_{uuid.uuid4().hex[:12]}"
        display = _clean(name, MAX_NAME, "新项目")
        workspace = self.projects_dir / project_id
        (workspace / "files").mkdir(parents=True, exist_ok=True)
        (workspace / "artifacts").mkdir(parents=True, exist_ok=True)
        now = _now()
        (workspace / "project.json").write_text(
            json.dumps(
                {
                    "id": project_id,
                    "kind": kind,
                    "name": display,
                    "created_at": now,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO projects
                  (id, kind, name, description, workspace, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, kind, display, description or "", str(workspace), now, now),
            )
        row = self.get_project(project_id)
        assert row is not None
        return row

    def list_projects(
        self, kind: str | None = None, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        if kind is not None and kind not in PROJECT_KINDS:
            raise ValueError("invalid project kind")
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT p.*,
                  (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id)
                    AS session_count,
                  (SELECT COUNT(*) FROM sessions s
                    WHERE s.project_id = p.id AND s.status = 'running')
                    AS running_count
                FROM projects p
                WHERE (? IS NULL OR p.kind = ?)
                  AND (? = 1 OR p.archived = 0)
                ORDER BY p.updated_at DESC, p.created_at DESC
                LIMIT 100
                """,
                (kind, kind, 1 if include_archived else 0),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT p.*,
                  (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id)
                    AS session_count,
                  (SELECT COUNT(*) FROM sessions s
                    WHERE s.project_id = p.id AND s.status = 'running')
                    AS running_count
                FROM projects p
                WHERE p.id = ?
                """,
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_project(self, project_id: str, **patch: Any) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        if "name" in patch:
            fields.append("name = ?")
            values.append(_clean(patch.get("name"), MAX_NAME, "新项目"))
        if "description" in patch:
            fields.append("description = ?")
            values.append(str(patch.get("description") or ""))
        if "research_question" in patch:
            fields.append("research_question = ?")
            values.append(str(patch.get("research_question") or ""))
        if "instructions" in patch:
            fields.append("instructions = ?")
            values.append(str(patch.get("instructions") or ""))
        if "archived" in patch:
            fields.append("archived = ?")
            values.append(1 if patch.get("archived") else 0)
        if not fields:
            return self.get_project(project_id)
        fields.append("updated_at = ?")
        values.append(_now())
        with self._db() as conn:
            cursor = conn.execute(
                f"UPDATE projects SET {', '.join(fields)} WHERE id = ?",
                (*values, project_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_project(project_id)

    def touch_project(self, project_id: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (_now(), project_id),
            )

    def workspace(self, project_id: str) -> Path:
        project = self.get_project(project_id)
        if project is None:
            raise LookupError("project not found")
        return Path(project["workspace"])

    # ── 项目文件树 ──────────────────────────────────────

    def list_items(self, project_id: str) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_items
                WHERE project_id = ?
                ORDER BY CASE kind WHEN 'folder' THEN 0 ELSE 1 END,
                         title COLLATE NOCASE
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM project_items WHERE id = ?", (item_id,)
            ).fetchone()
        return dict(row) if row else None

    def add_item(
        self,
        project_id: str,
        kind: str,
        title: str,
        *,
        parent_id: str | None = None,
        path: str | None = None,
        ref_id: str | None = None,
        mime: str = "",
        size: int = 0,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in ITEM_KINDS:
            raise ValueError("invalid item kind")
        if self.get_project(project_id) is None:
            raise LookupError("project not found")
        parent_path = ""
        with self._db() as conn:
            if parent_id:
                parent = conn.execute(
                    "SELECT * FROM project_items WHERE id = ? AND project_id = ?",
                    (parent_id, project_id),
                ).fetchone()
                if parent is None:
                    raise LookupError("parent folder not found")
                if parent["kind"] != "folder":
                    raise ValueError("parent is not a folder")
                parent_path = str(parent["path"] or "")
            display = _clean(title, MAX_TITLE, "未命名")
            item_path = (path or "").strip().lstrip("/")
            if not item_path:
                # 文件夹名会直接变成磁盘目录名，必须先清理非法字符。
                display = _clean(safe_name(title, "未命名"), MAX_TITLE, "未命名")
                item_path = f"{parent_path}/{display}" if parent_path else display
            item_id = f"i_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO project_items
                  (id, project_id, parent_id, kind, ref_id, path, title,
                   mime, size, meta, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    project_id,
                    parent_id,
                    kind,
                    ref_id,
                    item_path,
                    display,
                    mime or "",
                    int(size or 0),
                    json.dumps(meta or {}, ensure_ascii=False),
                    _now(),
                ),
            )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (_now(), project_id),
            )
        row = self.get_item(item_id)
        assert row is not None
        return row

    def create_folder(
        self, project_id: str, name: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        return self.add_item(
            project_id, "folder", name, parent_id=parent_id
        )

    def add_file(
        self,
        project_id: str,
        title: str,
        relative_path: str,
        *,
        parent_id: str | None = None,
        mime: str = "",
        size: int = 0,
        kind: str = "file",
    ) -> dict[str, Any]:
        return self.add_item(
            project_id,
            kind,
            title,
            parent_id=parent_id,
            path=relative_path,
            mime=mime,
            size=size,
        )

    def item_path(self, project_id: str, item: dict[str, Any]) -> Path:
        relative = str(item.get("path") or "")
        return resolve_in_files(self.workspace(project_id), relative)

    def remove_item(self, item_id: str) -> bool:
        item = self.get_item(item_id)
        if item is None:
            return False
        project_id = str(item["project_id"])
        with self._db() as conn:
            rows = conn.execute(
                """
                WITH RECURSIVE sub(id) AS (
                  SELECT id FROM project_items WHERE id = ?
                  UNION ALL
                  SELECT i.id FROM project_items i
                    JOIN sub ON i.parent_id = sub.id
                )
                SELECT i.kind, i.path FROM project_items i
                  JOIN sub ON i.id = sub.id
                """,
                (item_id,),
            ).fetchall()
            paths = [
                str(row["path"] or "")
                for row in rows
                if row["kind"] in ("file", "paper") and row["path"]
            ]
            folder_paths = [
                str(row["path"] or "")
                for row in rows
                if row["kind"] == "folder" and row["path"]
            ]
            conn.execute("DELETE FROM project_items WHERE id = ?", (item_id,))
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (_now(), project_id),
            )
        workspace = self.workspace(project_id)
        for relative in paths:
            remove_file(workspace, relative)
        # 目录自下而上删除，避免留下空文件夹。
        for relative in sorted(
            folder_paths, key=lambda value: value.count("/"), reverse=True
        ):
            try:
                target = resolve_in_files(workspace, relative)
            except ValueError:
                continue
            shutil.rmtree(target, ignore_errors=True)
        return True

    def _descendants(self, conn: Any, item_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            WITH RECURSIVE sub(id) AS (
              SELECT id FROM project_items WHERE id = ?
              UNION ALL
              SELECT i.id FROM project_items i JOIN sub ON i.parent_id = sub.id
            )
            SELECT i.* FROM project_items i JOIN sub ON i.id = sub.id
            WHERE i.id <> ?
            """,
            (item_id, item_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _is_inside(self, item_id: str | None, ancestor_id: str) -> bool:
        """item 是否是 ancestor 的子孙（用于阻止把文件夹拖进自己）。"""
        current = item_id
        seen: set[str] = set()
        while current:
            if current == ancestor_id:
                return True
            if current in seen:
                return False
            seen.add(current)
            row = self.get_item(current)
            current = row.get("parent_id") if row else None
        return False

    def _move_on_disk(
        self, project_id: str, old_relative: str, new_relative: str
    ) -> None:
        if not old_relative or old_relative == new_relative:
            return
        workspace = self.workspace(project_id)
        old_path = resolve_in_files(workspace, old_relative)
        new_path = resolve_in_files(workspace, new_relative)
        if not old_path.exists():
            # paper/note 等只在库里有记录时无需动盘。
            return
        if new_path.exists():
            raise ValueError("目标位置已存在同名文件或文件夹")
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_path), str(new_path))

    def update_item(
        self,
        project_id: str,
        item_id: str,
        *,
        name: str | None = None,
        parent_id: str | None | object = _UNSET,
    ) -> list[dict[str, Any]]:
        """重命名或移动文件项；返回该项目最新的完整文件列表。"""
        item = self.get_item(item_id)
        if item is None or item["project_id"] != project_id:
            raise LookupError("item not found")

        old_path = str(item.get("path") or "")
        parent_changed = parent_id is not _UNSET
        target_parent = (
            parent_id if parent_changed else item.get("parent_id")
        )
        if target_parent == item_id:
            raise ValueError("不能把文件夹移动到自身")
        if parent_changed and target_parent and self._is_inside(str(target_parent), item_id):
            raise ValueError("不能把文件夹移动到自己的子目录")

        if name is not None:
            title = _clean(
                safe_name(name, "未命名"), MAX_TITLE, item["title"]
            )
        else:
            title = str(item["title"])

        if parent_changed:
            if target_parent:
                parent = self.get_item(str(target_parent))
                if parent is None or parent["project_id"] != project_id:
                    raise LookupError("parent folder not found")
                if parent["kind"] != "folder":
                    raise ValueError("parent is not a folder")
                parent_path = str(parent["path"] or "")
            else:
                parent_path = ""
        else:
            parent_path = old_path.rsplit("/", 1)[0] if "/" in old_path else ""

        new_path = f"{parent_path}/{title}" if parent_path else title

        self._move_on_disk(project_id, old_path, new_path)

        with self._db() as conn:
            conn.execute(
                "UPDATE project_items SET parent_id = ?, title = ?, path = ? WHERE id = ?",
                (target_parent, title, new_path, item_id),
            )
            if (
                item["kind"] == "folder"
                and old_path
                and old_path != new_path
            ):
                old_prefix = old_path.rstrip("/") + "/"
                new_prefix = new_path.rstrip("/") + "/"
                for child in self._descendants(conn, item_id):
                    child_path = str(child.get("path") or "")
                    if child_path.startswith(old_prefix):
                        conn.execute(
                            "UPDATE project_items SET path = ? WHERE id = ?",
                            (new_prefix + child_path[len(old_prefix):], child["id"]),
                        )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (_now(), project_id),
            )
        return self.list_items(project_id)

    def _unique_title(
        self,
        conn: Any,
        project_id: str,
        parent_id: str | None,
        title: str,
    ) -> str:
        rows = conn.execute(
            """
            SELECT title FROM project_items
            WHERE project_id = ? AND IFNULL(parent_id, '') = IFNULL(?, '')
            """,
            (project_id, parent_id),
        ).fetchall()
        existing = {str(row["title"]) for row in rows}
        if title not in existing:
            return title
        path = Path(title)
        stem, suffix = path.stem, path.suffix
        for index in range(1, 1000):
            candidate = f"{stem}-{index}{suffix}"
            if candidate not in existing:
                return candidate
        return f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"

    def copy_item(
        self,
        project_id: str,
        item_id: str,
        parent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """复制文件或文件夹（递归），返回项目最新文件列表。"""
        item = self.get_item(item_id)
        if item is None or item["project_id"] != project_id:
            raise LookupError("item not found")
        if parent_id and (
            parent_id == item_id or self._is_inside(parent_id, item_id)
        ):
            raise ValueError("不能把文件夹复制到自己的子目录")
        if parent_id:
            parent = self.get_item(parent_id)
            if parent is None or parent["project_id"] != project_id:
                raise LookupError("parent folder not found")
            if parent["kind"] != "folder":
                raise ValueError("parent is not a folder")
            parent_path = str(parent["path"] or "")

        with self._db() as conn:
            title = self._unique_title(conn, project_id, parent_id, str(item["title"]))
            children = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM project_items WHERE parent_id = ?", (item_id,)
                ).fetchall()
            ]

        workspace = self.workspace(project_id)
        new_item = self.add_item(
            project_id,
            str(item["kind"]),
            title,
            parent_id=parent_id,
            ref_id=item.get("ref_id"),
            mime=str(item.get("mime") or ""),
            size=int(item.get("size") or 0),
            meta=json.loads(item.get("meta") or "{}"),
        )
        old_path = str(item.get("path") or "")
        new_path = str(new_item.get("path") or "")
        if item["kind"] == "folder":
            resolve_in_files(workspace, new_path).mkdir(parents=True, exist_ok=True)
        elif old_path and new_path:
            source = resolve_in_files(workspace, old_path)
            if source.is_file():
                target = resolve_in_files(workspace, new_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

        # 子项按 parent 关系逐个复制（磁盘文件随子项一起复制）。
        for child in children:
            self.copy_item(project_id, str(child["id"]), str(new_item["id"]))
        return self.list_items(project_id)

    # ── 会话索引 ────────────────────────────────────────

    def create_session(
        self,
        project_id: str,
        title: str | None = None,
        model: str = "deepseek-v4-flash",
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if project is None:
            raise LookupError("project not found")
        if project.get("archived"):
            raise PermissionError("project archived")
        session_id = f"s_{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                  (id, project_id, title, model, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'idle', ?, ?)
                """,
                (
                    session_id,
                    project_id,
                    _clean(title, MAX_NAME, "新会话"),
                    model or "deepseek-v4-flash",
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (now, project_id),
            )
        row = self.get_session(session_id)
        assert row is not None
        return row

    def list_sessions(self, project_id: str) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE project_id = ?
                ORDER BY updated_at DESC, created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_session(self, session_id: str, **patch: Any) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        if "title" in patch:
            fields.append("title = ?")
            values.append(_clean(patch.get("title"), MAX_NAME, "新会话"))
        if "status" in patch:
            fields.append("status = ?")
            values.append(str(patch.get("status") or "idle"))
        if "last_turn_at" in patch:
            fields.append("last_turn_at = ?")
            values.append(patch.get("last_turn_at"))
        if not fields:
            return self.get_session(session_id)
        fields.append("updated_at = ?")
        values.append(_now())
        with self._db() as conn:
            cursor = conn.execute(
                f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?",
                (*values, session_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE projects SET updated_at = ? WHERE id = ?",
                    (_now(), row["project_id"]),
                )
        return self.get_session(session_id)

    # ── 产物索引 ────────────────────────────────────────

    def add_artifact(
        self,
        project_id: str,
        title: str,
        kind: str,
        path: str,
        *,
        session_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.get_project(project_id) is None:
            raise LookupError("project not found")
        artifact_id = f"a_{uuid.uuid4().hex[:12]}"
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO artifacts
                  (id, project_id, session_id, path, kind, title, version,
                   created_at, meta)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    session_id,
                    path,
                    _clean(kind, 40, "note"),
                    _clean(title, MAX_TITLE, "未命名产物"),
                    _now(),
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (_now(), project_id),
            )
        row = self.get_artifact(artifact_id)
        assert row is not None
        return row

    def list_artifacts(
        self, project_id: str, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE project_id = ? AND (? IS NULL OR session_id = ?)
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (project_id, session_id, session_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return dict(row) if row else None

    # ── 会话事件 ────────────────────────────────────────
    def list_events(
        self, session_id: str, after: int = 0, limit: int = 2000
    ) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT seq, at, data FROM session_events
                WHERE session_id = ? AND seq > ?
                ORDER BY seq
                LIMIT ?
                """,
                (session_id, max(0, int(after)), limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except ValueError:
                data = {}
            events.append({"seq": int(row["seq"]), "at": int(row["at"]), **data})
        return events

    def last_agent_seq(self, session_id: str) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT agent_seq FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return int(row["agent_seq"] or 0) if row else 0

    def append_event(
        self, session_id: str, event: dict[str, Any]
    ) -> dict[str, Any]:
        """产品事件（用户消息、提示）：Python 自增 seq。"""
        return self._append_event(session_id, None, event)

    def append_agent_event(
        self, session_id: str, agent_seq: int, event: dict[str, Any]
    ) -> dict[str, Any]:
        """agent 事件：按 (agent_seq, at) 去重，两个标签页同时订阅也不会重复落库。"""
        return self._append_event(session_id, int(agent_seq) or None, event)

    def _append_event(
        self,
        session_id: str,
        agent_seq: int | None,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in event.items()
            if key not in {"seq", "at", "agentSeq"}
        }
        event_type = str(payload.get("type") or "notice")
        at = int(event.get("at") or time.time() * 1000)
        with self._db() as conn:
            if agent_seq is not None:
                row = conn.execute(
                    """
                    SELECT seq, at, data FROM session_events
                    WHERE session_id = ? AND agent_seq = ? AND at = ?
                    """,
                    (session_id, agent_seq, at),
                ).fetchone()
                if row is not None:
                    try:
                        existing = json.loads(row["data"])
                    except ValueError:
                        existing = {}
                    return {"seq": int(row["seq"]), "at": int(row["at"]), **existing}

            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM session_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["seq"] or 0) + 1
            conn.execute(
                """
                INSERT INTO session_events(session_id, seq, agent_seq, at, type, data)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    seq,
                    agent_seq,
                    at,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._apply_event_effects(conn, session_id, event_type, payload, agent_seq)
        return {"seq": seq, "at": at, **payload}

    def _apply_event_effects(
        self,
        conn: Any,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        agent_seq: int | None,
    ) -> None:
        now = _now()
        if agent_seq is not None:
            conn.execute(
                "UPDATE sessions SET agent_seq = MAX(agent_seq, ?) WHERE id = ?",
                (agent_seq, session_id),
            )
        if event_type == "title.update":
            title = _clean(str(payload.get("title") or ""), MAX_TITLE, "新会话")
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, session_id),
            )
        elif event_type == "turn.start":
            conn.execute(
                """
                UPDATE sessions
                SET status = 'running', last_turn_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, session_id),
            )
        elif event_type == "turn.end":
            conn.execute(
                "UPDATE sessions SET status = 'idle', updated_at = ? WHERE id = ?",
                (now, session_id),
            )
