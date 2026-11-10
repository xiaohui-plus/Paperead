"""学术阅读（Scholaread）本机数据同步。

只读写本机安装的 Scholaread 缓存库：

- 拉取：``reading_list`` 的题录与 ``files/pdf`` 里的缓存 PDF → Paperead 文献库；
  阅读进度按 ``read_point``（0–1）折算成 ``read_pct``。
- 写回：Paperead 的阅读进度 → ``reading_list``，并追加一条
  ``reading_list_events``（``synced=0``），让学术阅读自己的同步机制感知变化。

安全约束：

- 拉取先做一致性快照，再只读副本，绝不在学术阅读运行时改原库。
- 写回前自动备份；学术阅读进程在运行时直接拒绝写回。
- 只 UPDATE 已存在的行，不新增、不删除文献。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from paperead.env import env
from server.store import LibraryStore

log = logging.getLogger("paperead.scholaread")

MAX_PDF_BYTES = 200 * 1024 * 1024
BACKUP_KEEP = 10
MAX_ERRORS = 20


class SyncReq(BaseModel):
    data_dir: str = ""
    pull: bool = True
    push: bool = True
    limit: int = Field(500, ge=1, le=5000)


def default_data_dir() -> Path:
    """不配置也能找到：环境变量 → 各系统默认位置。"""
    explicit = env("PAPEREAD_SCHOLAREAD_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    home = Path.home()
    if os.name == "nt":
        base = os.getenv("APPDATA") or str(home / "AppData" / "Roaming")
        return (Path(base) / "scholaread").resolve()
    if sys.platform == "darwin":
        return (home / "Library" / "Application Support" / "scholaread").resolve()
    return (home / ".config" / "scholaread").resolve()


def _resolve_dir(value: str) -> Path:
    raw = (value or "").strip().strip('"')
    if not raw:
        return default_data_dir()
    path = Path(raw).expanduser()
    # 用户有可能直接填到 scholaread.sqlite
    if path.name.lower() == "scholaread.sqlite":
        path = path.parent
    return path.resolve()


def _db_path(data_dir: Path) -> Path:
    return data_dir / "scholaread.sqlite"


def _is_running() -> bool | None:
    """学术阅读是否正在运行；非 Windows 或检测失败时返回 None（未知）。"""
    if os.name != "nt":
        return None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Scholaread.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
        return "scholaread.exe" in (out.stdout or "").lower()
    except Exception:
        return None


def _schema_ok(con: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in con.execute("select name from sqlite_master where type='table'")
    }
    if not {"reading_list", "reading_list_events"} <= tables:
        return False
    cols = {str(row[1]) for row in con.execute("pragma table_info(reading_list)")}
    return {"reading_id", "read_point", "read_progress"} <= cols


def _open_snapshot(db: Path) -> tuple[sqlite3.Connection, Path]:
    """生成一致性只读副本，避免干扰正在运行的学术阅读。"""
    tmp = Path(tempfile.mkdtemp(prefix="paperead-sr-"))
    dst = tmp / "scholaread.sqlite"
    try:
        src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=4)
        try:
            bak = sqlite3.connect(dst)
            try:
                src.backup(bak)
            finally:
                bak.close()
        finally:
            src.close()
    except Exception:
        # backup() 在 WAL 只读连接受限时可能失败，退回复制三件套。
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(db) + suffix)
            if source.is_file():
                shutil.copy2(source, Path(str(dst) + suffix))
    con = sqlite3.connect(dst, timeout=4)
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout=3000")
    return con, tmp


def _cleanup(con: sqlite3.Connection, tmp: Path) -> None:
    try:
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _point(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(str(value or "0").strip())))
    except (TypeError, ValueError):
        return 0.0


def _iso_ts(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _authors(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw[:300]
    if isinstance(parsed, list):
        names = []
        for item in parsed:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                names.append(str(item.get("name") or item.get("full_name") or ""))
        return ", ".join(name for name in names if name)[:600]
    return raw[:300]


def _norm_title(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").lower())


def _pdf_path(data_dir: Path, row: dict[str, Any]) -> Path | None:
    cache = str(row.get("cache_path") or "").strip()
    if cache:
        root = (data_dir / "files" / "pdf").resolve()
        candidate = (root / cache).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if candidate.is_file():
            return candidate
    raw = str(row.get("file_path") or "").strip()
    if raw:
        if raw.lower().startswith("file://"):
            raw = raw[7:]
        candidate = Path(raw).expanduser()
        if candidate.suffix.lower() == ".pdf" and candidate.is_file():
            return candidate
    return None


def _safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title or "paper").strip(" .")
    return f"{(name or 'paper')[:80]}.pdf"


def _find_existing(
    store: LibraryStore, reading_key: str, doi: str, title: str
) -> dict[str, Any] | None:
    hit = store.find_by_source(f"scholaread:{reading_key}")
    if hit:
        return hit
    rows = store.list(include_project=True)
    if doi.strip():
        target = doi.strip().lower()
        for row in rows:
            if str(row.get("doi") or "").strip().lower() == target:
                return row
    normalized = _norm_title(title)
    if len(normalized) >= 10:
        for row in rows:
            if _norm_title(str(row.get("title") or "")) == normalized:
                return row
    return None


def _entry_rows(con: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = con.execute(
        "select * from reading_list "
        "order by coalesce(update_at, '') desc, _id desc limit ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _reading_key(row: dict[str, Any]) -> str:
    return str(row.get("reading_id") or "").strip() or f"local-{row.get('_id')}"


def _pull(
    store: LibraryStore,
    data_dir: Path,
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    result: dict[str, Any] = {
        "scanned": len(rows),
        "imported": 0,
        "linked": 0,
        "progress_updated": 0,
        "skipped_no_pdf": 0,
        "errors": [],
    }
    new_ids: list[str] = []

    for row in rows:
        title = str(row.get("title") or row.get("name") or "").strip()
        try:
            key = _reading_key(row)
            doi = str(row.get("doi") or "").strip()
            remote_point = _point(row.get("read_point"))
            remote_at = _iso_ts(row.get("read_at")) or _iso_ts(row.get("update_at"))
            remote_progress = int(row.get("read_progress") or 0)
            existing = _find_existing(store, key, doi, title)

            if existing:
                patch: dict[str, Any] = {
                    "scholaread_id": key,
                    "scholaread_share_id": str(row.get("share_id") or ""),
                    "scholaread_updated_at": str(row.get("update_at") or ""),
                    "scholaread_read_point": remote_point,
                }
                if not existing.get("scholaread_id"):
                    result["linked"] += 1
                local_at = float(existing.get("read_at") or 0)
                if remote_at > local_at + 2 and (remote_point > 0 or remote_progress > 0):
                    patch.update(
                        read_pct=round(remote_point * 100, 1),
                        read_block="",
                        read_page=remote_progress,
                        read_at=remote_at,
                        scholaread_read_at=str(row.get("read_at") or ""),
                    )
                    result["progress_updated"] += 1
                store.update(str(existing["id"]), **patch)
                continue

            pdf = _pdf_path(data_dir, row)
            if pdf is None:
                result["skipped_no_pdf"] += 1
                continue
            if pdf.stat().st_size > MAX_PDF_BYTES:
                result["skipped_no_pdf"] += 1
                continue

            meta = {
                "source": "scholaread",
                "source_id": f"scholaread:{key}",
                "title": title or pdf.stem,
                "authors": _authors(row.get("authors")),
                "doi": doi,
                "arxiv_id": str(row.get("arxiv_id") or ""),
                "url": str(row.get("url") or row.get("link") or ""),
                "tags": str(row.get("tags") or ""),
            }
            record = store.add(_safe_filename(meta["title"]), pdf.read_bytes(), meta)
            patch = {
                "scholaread_id": key,
                "scholaread_share_id": str(row.get("share_id") or ""),
                "scholaread_updated_at": str(row.get("update_at") or ""),
                "scholaread_read_point": remote_point,
            }
            if remote_point > 0 or remote_progress > 0:
                patch.update(
                    read_pct=round(remote_point * 100, 1),
                    read_block="",
                    read_page=remote_progress,
                    read_at=remote_at,
                    scholaread_read_at=str(row.get("read_at") or ""),
                )
            store.update(str(record["id"]), **patch)
            new_ids.append(str(record["id"]))
            result["imported"] += 1
        except Exception as exc:  # 单篇失败不影响其余论文
            if len(result["errors"]) < MAX_ERRORS:
                result["errors"].append(
                    {"title": title[:80], "error": str(exc)[:160]}
                )

    return result, new_ids


def _backup(db: Path, root: Path) -> Path:
    out_dir = root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"scholaread-{stamp}.sqlite"
    src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=4)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    backups = sorted(out_dir.glob("scholaread-*.sqlite"))
    for old in backups[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return dest


def _device_id(con: sqlite3.Connection) -> str:
    row = con.execute(
        "select device_id from reading_list_events where device_id != '' "
        "order by rowid desc limit 1"
    ).fetchone()
    return str(row[0]) if row and row[0] else "paperead"


def _cutoff_point(pct: float) -> float:
    return max(0.0, min(1.0, pct / 100.0))


def _push(
    store: LibraryStore, data_dir: Path
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "updated": 0,
        "skipped": 0,
        "backup": "",
        "error": "",
    }
    if _is_running():
        result["error"] = "学术阅读正在运行，退出后会自动写回进度"
        return result

    rows = [
        row
        for row in store.list(include_project=True)
        if row.get("scholaread_id") and float(row.get("read_pct") or 0) > 0
    ]
    if not rows:
        return result

    db = _db_path(data_dir)
    if not db.is_file():
        result["error"] = "未找到学术阅读数据库"
        return result

    try:
        backup = _backup(db, store.root)
        result["backup"] = str(backup)
    except Exception as exc:
        result["error"] = f"备份失败，已取消写回：{exc}"
        return result

    con = sqlite3.connect(str(db), timeout=4)
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout=3000")
    try:
        con.execute("begin immediate")
        device = _device_id(con)
        now = _iso(time.time())
        for row in rows:
            reading_id = str(row.get("scholaread_id") or "")
            remote = con.execute(
                "select _id, reading_id, read_mode, read_progress, read_point, "
                "total_num, read_state, read_at from reading_list "
                "where reading_id = ?",
                (reading_id,),
            ).fetchone()
            if remote is None:
                result["skipped"] += 1
                continue

            pct = float(row.get("read_pct") or 0)
            local_at = float(row.get("read_at") or 0)
            remote_at = _iso_ts(remote["read_at"])
            remote_point = _point(remote["read_point"])
            if local_at <= 0 or local_at <= remote_at + 2:
                result["skipped"] += 1
                continue
            if abs(pct - remote_point * 100) <= 0.5:
                result["skipped"] += 1
                continue

            point = _cutoff_point(pct)
            total = int(remote["total_num"] or 0)
            progress = max(1, round(point * total)) if total else int(
                remote["read_progress"] or 1
            )
            read_at = _iso(local_at)
            con.execute(
                "update reading_list set read_progress = ?, read_point = ?, "
                "read_at = ?, update_at = ? where reading_id = ?",
                (progress, f"{point:.5f}", read_at, now, reading_id),
            )
            fields = [
                "read_mode",
                "read_progress",
                "read_point",
                "total_num",
                "read_state",
                "read_at",
            ]
            data = {
                "read_mode": remote["read_mode"] or "web:reflow",
                "read_progress": progress,
                "read_point": f"{point:.5f}",
                "total_num": total,
                "read_state": remote["read_state"] or "doing",
                "read_at": read_at,
            }
            event_id = str(int(time.time() * 1000))
            while con.execute(
                "select 1 from reading_list_events where client_event_id = ?",
                (event_id,),
            ).fetchone():
                event_id = str(int(event_id) + 1)
            con.execute(
                "insert into reading_list_events (client_event_id, event_id, "
                "event_at, device_id, op, op_at, data_id, data_local_row_id, "
                "data_fields, data, sign, synced, sync_result_code, "
                "sync_result_message) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    "",
                    "",
                    device,
                    "update",
                    _iso(time.time()),
                    reading_id,
                    int(remote["_id"]),
                    json.dumps(fields, ensure_ascii=False),
                    json.dumps(data, ensure_ascii=False),
                    "",
                    0,
                    0,
                    "",
                ),
            )
            result["updated"] += 1
        con.commit()
    except Exception as exc:
        con.rollback()
        result["error"] = f"写回失败，已回滚：{exc}"
    finally:
        con.close()
    return result


def _status(data_dir: Path) -> dict[str, Any]:
    db = _db_path(data_dir)
    out: dict[str, Any] = {
        "ok": False,
        "data_dir": str(data_dir),
        "db_path": str(db),
        "exists": db.is_file(),
        "schema_ok": False,
        "running": _is_running(),
        "total": 0,
        "with_pdf": 0,
        "notes": 0,
        "error": "",
    }
    if not db.is_file():
        out["error"] = "未找到学术阅读数据，请先安装并登录学术阅读"
        return out

    try:
        con, tmp = _open_snapshot(db)
        try:
            out["schema_ok"] = _schema_ok(con)
            if not out["schema_ok"]:
                out["error"] = "学术阅读数据库版本不兼容"
                return out
            out["total"] = int(
                con.execute("select count(*) from reading_list").fetchone()[0]
            )
            out["with_pdf"] = sum(
                1
                for row in con.execute("select * from reading_list")
                if _pdf_path(data_dir, dict(row)) is not None
            )
            try:
                out["notes"] = int(
                    con.execute("select count(*) from notes").fetchone()[0]
                )
            except sqlite3.Error:
                out["notes"] = 0
            out["ok"] = True
        finally:
            _cleanup(con, tmp)
    except Exception as exc:
        out["error"] = f"读取学术阅读数据失败：{exc}"
    return out


def _run_sync(
    store: LibraryStore,
    data_dir_raw: str,
    pull: bool,
    push: bool,
    limit: int,
) -> tuple[dict[str, Any], list[str]]:
    started = time.time()
    data_dir = _resolve_dir(data_dir_raw)
    db = _db_path(data_dir)
    running = _is_running()
    result: dict[str, Any] = {
        "ok": False,
        "data_dir": str(data_dir),
        "running": running,
        "pull": {
            "scanned": 0,
            "imported": 0,
            "linked": 0,
            "progress_updated": 0,
            "skipped_no_pdf": 0,
            "errors": [],
        },
        "push": {"updated": 0, "skipped": 0, "backup": "", "error": ""},
        "warnings": [],
        "duration_ms": 0,
        "error": "",
    }
    new_ids: list[str] = []

    if not db.is_file():
        result["error"] = "未找到学术阅读数据，请先安装并登录学术阅读"
        result["duration_ms"] = int((time.time() - started) * 1000)
        return result, new_ids

    try:
        con, tmp = _open_snapshot(db)
        try:
            if not _schema_ok(con):
                result["error"] = "学术阅读数据库版本不兼容"
                result["duration_ms"] = int((time.time() - started) * 1000)
                return result, new_ids
            rows = _entry_rows(con, limit)
            if pull:
                pulled, new_ids = _pull(store, data_dir, rows)
                result["pull"] = pulled
        finally:
            _cleanup(con, tmp)
    except Exception as exc:
        result["error"] = f"读取学术阅读数据失败：{exc}"
        result["duration_ms"] = int((time.time() - started) * 1000)
        return result, new_ids

    if push:
        result["push"] = _push(store, data_dir)
        if result["push"].get("error"):
            result["warnings"].append(result["push"]["error"])

    result["ok"] = not result["error"]
    result["duration_ms"] = int((time.time() - started) * 1000)
    return result, new_ids


def build_router(
    store: LibraryStore, schedule_parse: Callable[[str], None]
) -> APIRouter:
    router = APIRouter(prefix="/api/scholaread", tags=["scholaread"])
    lock = asyncio.Lock()

    @router.get("/status")
    async def status(data_dir: str = Query("")) -> dict[str, Any]:
        return await asyncio.to_thread(_status, _resolve_dir(data_dir))

    @router.post("/sync")
    async def sync(req: SyncReq) -> dict[str, Any]:
        async with lock:
            data, new_ids = await asyncio.to_thread(
                _run_sync, store, req.data_dir, req.pull, req.push, req.limit
            )
        for paper_id in new_ids:
            schedule_parse(paper_id)
        return data

    return router
