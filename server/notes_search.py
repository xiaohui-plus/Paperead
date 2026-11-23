"""外部笔记检索：独立于论文检索索引。

论文检索（`server.research.search.LibrarySearch`）绑定 `LibraryStore` 与
blocks.json，直接扩会把两类内容混进同一结果集。这里用同样的 FTS5 trigram
思路单独建库，查询入口也单独暴露。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from server.notes_store import NoteStore

INDEX_VERSION = 1

_TOKEN_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.U)


def _fts_query(text: str) -> str:
    words = [word for word in _TOKEN_RE.split(text) if word]
    if not words:
        return '""'
    # 每个词加引号并做前缀匹配，避免 FTS5 把 ":" "-" 当语法。
    return " AND ".join(f'"{word}"*' for word in words)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class NoteSearch:
    def __init__(self, store: NoteStore, db_path: Path):
        self.store = store
        self.db_path = Path(db_path)
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _stamp(self) -> str:
        parts: list[str] = [f"v{INDEX_VERSION}"]
        if self.store.root.exists():
            for path in sorted(self.store.root.glob("n_*.json")):
                parts.append(f"{path.stem}:{path.stat().st_mtime_ns}")
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
              note_id UNINDEXED, conference, field, title, description, tags,
              tokenize='trigram'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS note_block_fts USING fts5(
              note_id UNINDEXED, block_id UNINDEXED, section, kind, text,
              tokenize='trigram'
            );
            """
        )

    def refresh(self, force: bool = False) -> None:
        stamp = self._stamp()
        with self._lock, self._connect() as conn:
            self._ensure_schema(conn)
            row = conn.execute("SELECT value FROM meta WHERE key = 'stamp'").fetchone()
            if not force and row and row["value"] == stamp:
                return
            conn.execute("DELETE FROM note_fts")
            conn.execute("DELETE FROM note_block_fts")
            for meta in self.store.list():
                note_id = str(meta.get("id") or "")
                if not note_id:
                    continue
                conn.execute(
                    "INSERT INTO note_fts (note_id, conference, field, title, description, tags)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        note_id,
                        str(meta.get("conference") or ""),
                        str(meta.get("field") or ""),
                        str(meta.get("title") or ""),
                        str(meta.get("description") or ""),
                        " ".join(str(tag) for tag in (meta.get("tags") or [])),
                    ),
                )
                full = self.store.get(note_id) or {}
                for block in full.get("blocks") or []:
                    text = str(block.get("text") or "").strip()
                    if not text:
                        continue
                    conn.execute(
                        "INSERT INTO note_block_fts (note_id, block_id, section, kind, text)"
                        " VALUES (?,?,?,?,?)",
                        (
                            note_id,
                            str(block.get("id") or ""),
                            str(block.get("section") or ""),
                            str(block.get("kind") or ""),
                            text,
                        ),
                    )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('stamp', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (stamp,),
            )
            conn.commit()

    def search(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        text = query.strip()
        if not text:
            return []
        limit = max(1, min(int(limit), 50))
        self.refresh()
        words = [word for word in _TOKEN_RE.split(text) if word]
        if not words:
            return []

        with self._lock, self._connect() as conn:
            note_ids: set[str] = set()
            if len(text) >= 3:
                expr = _fts_query(text)
                for row in conn.execute(
                    "SELECT note_id FROM note_fts WHERE note_fts MATCH ?", (expr,)
                ):
                    note_ids.add(str(row["note_id"]))
                for row in conn.execute(
                    "SELECT note_id FROM note_block_fts WHERE note_block_fts MATCH ?",
                    (expr,),
                ):
                    note_ids.add(str(row["note_id"]))
            else:
                # trigram 至少 3 个字符；两字中文查询退回 LIKE 扫标题与正文。
                like = f"%{_like_escape(text)}%"
                for row in conn.execute(
                    "SELECT note_id FROM note_fts "
                    "WHERE title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'",
                    (like, like),
                ):
                    note_ids.add(str(row["note_id"]))
                for row in conn.execute(
                    "SELECT DISTINCT note_id FROM note_block_fts "
                    "WHERE text LIKE ? ESCAPE '\\'",
                    (like,),
                ):
                    note_ids.add(str(row["note_id"]))

        results: list[dict[str, Any]] = []
        for note_id in note_ids:
            meta = self.store.get(note_id) or {}
            title = str(meta.get("title") or "")
            description = str(meta.get("description") or "")
            tags = " ".join(str(tag) for tag in (meta.get("tags") or []))
            blocks = meta.get("blocks") or []

            snippet = ""
            for block in blocks:
                block_text = str(block.get("text") or "")
                if block_text and any(word.lower() in block_text.lower() for word in words):
                    snippet = block_text
                    break

            haystack = " ".join([title, description, tags] + [
                str(block.get("text") or "") for block in blocks
            ]).lower()
            coverage = sum(1 for word in words if word.lower() in haystack) / len(words)
            if not coverage:
                continue
            # 标题命中比正文命中更可信；覆盖度相同的时候用它打破平手。
            title_hits = sum(1 for word in words if word.lower() in title.lower())
            results.append(
                {
                    "id": note_id,
                    "title": title,
                    "conference": str(meta.get("conference") or ""),
                    "field": str(meta.get("field") or ""),
                    "tags": meta.get("tags") or [],
                    "snippet": (snippet or description)[:200],
                    "score": round(coverage + title_hits * 0.25, 3),
                }
            )

        results.sort(key=lambda entry: entry["score"], reverse=True)
        return results[:limit]
