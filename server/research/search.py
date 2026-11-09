"""本地文献库全文检索（SQLite FTS5 trigram）。

索引放在 `data/research/search.db`，按 library.json 与 blocks 文件的
修改时间做增量刷新；检索结果最多同页 2 段，避免一段话刷屏。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from server.store import LibraryStore

MAX_SNIPPET = 220
PER_PAGE = 2
INDEX_VERSION = 2  # 摘要摘录/去重等索引语义变化时递增，触发重建
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(text: str) -> str:
    """把查询转成 FTS5 表达式。

    英文多词查询按词 OR（bm25 会把命中更多词的结果排前），避免整句精确短语
    在长查询下几乎总是 0 命中；中文与单词查询保持精确短语。
    """
    cleaned = text.strip()
    if CJK_RE.search(cleaned) or " " not in cleaned:
        return '"' + cleaned.replace('"', '""') + '"'
    tokens = [token for token in re.split(r"\s+", cleaned) if len(token) >= 3]
    if not tokens:
        return '"' + cleaned.replace('"', '""') + '"'
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def _lead_text(blocks: list[dict[str, Any]], limit: int = 700) -> str:
    """没有正式摘要时，用正文开头一段作为检索摘录。"""
    parts: list[str] = []
    total = 0
    for block in blocks:
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= limit:
            break
    return " ".join(parts)[:limit]


def _snippet(text: str, query: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= MAX_SNIPPET:
        return compact
    needle = query.strip()
    hit = compact.lower().find(needle.lower())
    if hit < 0 and " " in needle and not CJK_RE.search(needle):
        # 多词 OR 查询通常找不到整句，退而定位最长的那个词。
        longest = max(needle.split(), key=len, default="")
        hit = compact.lower().find(longest.lower()) if longest else -1
    if hit < 0:
        return compact[:MAX_SNIPPET] + "…"
    start = max(0, hit - 60)
    end = min(len(compact), start + MAX_SNIPPET)
    return ("…" if start else "") + compact[start:end] + ("…" if end < len(compact) else "")


class LibrarySearch:
    def __init__(self, library: LibraryStore, db_path: Path):
        self.library = library
        self.db_path = db_path
        self._lock = threading.Lock()

    # ── 索引 ────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _stamp(self) -> str:
        parts: list[str] = [f"v{INDEX_VERSION}"]
        index = self.library.index
        parts.append(str(index.stat().st_mtime_ns if index.exists() else 0))
        for row in self.library.list(include_project=True):
            blocks = self.library.blocks_path(row["id"])
            parts.append(
                f"{row['id']}:{row.get('project_id') or ''}:"
                f"{blocks.stat().st_mtime_ns if blocks.exists() else 0}"
            )
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_fts)")}
        if cols and "project_id" not in cols:
            # 索引是可重建的缓存；加项目作用域时直接重建，省掉迁移脚本。
            conn.executescript("DROP TABLE IF EXISTS paper_fts; DROP TABLE IF EXISTS block_fts;")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5(
              paper_id UNINDEXED, project_id UNINDEXED, title, authors, abstract,
              tokenize='trigram'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(
              paper_id UNINDEXED, project_id UNINDEXED, block_id UNINDEXED, page UNINDEXED,
              text, tokenize='trigram'
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
            conn.execute("DELETE FROM paper_fts")
            conn.execute("DELETE FROM block_fts")
            for paper in self.library.list(include_project=True):
                project_id = paper.get("project_id") or ""
                blocks = self.library.read_blocks(paper["id"]) or []
                abstract = str(paper.get("abstract") or "").strip()
                if not abstract:
                    # 老数据没有题录摘要，用正文开头做检索摘录，避免模型为判断相关性逐篇 paper_read。
                    abstract = _lead_text(blocks)
                conn.execute(
                    "INSERT INTO paper_fts (paper_id, project_id, title, authors, abstract)"
                    " VALUES (?,?,?,?,?)",
                    (
                        paper["id"],
                        project_id,
                        paper.get("title") or "",
                        " ".join(paper.get("authors") or []),
                        abstract,
                    ),
                )
                for block in blocks:
                    text = (block.get("text") or "").strip()
                    if not text:
                        continue
                    conn.execute(
                        "INSERT INTO block_fts (paper_id, project_id, block_id, page, text)"
                        " VALUES (?,?,?,?,?)",
                        (
                            paper["id"],
                            project_id,
                            block.get("id") or "",
                            int(block.get("page") or 1),
                            text,
                        ),
                    )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('stamp', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (stamp,),
            )
            conn.commit()

    # ── 查询 ────────────────────────────────────────────

    @staticmethod
    def _scope(project_id: str | None) -> tuple[str, tuple[str, ...]]:
        """全局文献始终可见；传了项目 id 时再加上该项目的论文。"""
        if project_id:
            return "(project_id = '' OR project_id = ?)", (project_id,)
        return "project_id = ''", ()

    def _papers(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._scope(project_id)
        if len(query) >= 3:
            rows = conn.execute(
                "SELECT paper_id, title, authors, abstract, bm25(paper_fts) AS score "
                f"FROM paper_fts WHERE paper_fts MATCH ? AND {where} ORDER BY score LIMIT ?",
                (_fts_query(query), *params, limit),
            ).fetchall()
        else:
            like = f"%{_like_escape(query)}%"
            rows = conn.execute(
                "SELECT paper_id, title, authors, abstract, 0 AS score FROM paper_fts "
                f"WHERE {where} AND (title LIKE ? ESCAPE '\\' OR abstract LIKE ? ESCAPE '\\') "
                "LIMIT ?",
                (*params, like, like, limit),
            ).fetchall()
        return [
            {
                "paperId": row["paper_id"],
                "title": row["title"],
                "authors": [a for a in (row["authors"] or "").split(" ") if a],
                "page": None,
                "blockId": None,
                "abstract": (row["abstract"] or "").strip()[:700],
                "hasAbstract": bool((row["abstract"] or "").strip()),
                "snippet": _snippet(row["abstract"] or row["title"], query),
                "score": round(float(row["score"]), 4),
            }
            for row in rows
        ]

    def _blocks(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._scope(project_id)
        if len(query) >= 3:
            rows = conn.execute(
                "SELECT paper_id, block_id, page, text, bm25(block_fts) AS score "
                f"FROM block_fts WHERE block_fts MATCH ? AND {where} ORDER BY score LIMIT ?",
                (_fts_query(query), *params, limit * 4),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT paper_id, block_id, page, text, 0 AS score FROM block_fts "
                f"WHERE {where} AND text LIKE ? ESCAPE '\\' LIMIT ?",
                (*params, f"%{_like_escape(query)}%", limit * 4),
            ).fetchall()

        results: list[dict[str, Any]] = []
        seen_pages: dict[tuple[str, int], int] = {}
        per_paper: dict[str, int] = {}
        for row in rows:
            key = (row["paper_id"], int(row["page"]))
            if seen_pages.get(key, 0) >= PER_PAGE:
                continue
            if per_paper.get(row["paper_id"], 0) >= 3:
                continue
            paper = self.library.get(row["paper_id"]) or {}
            abstract = str(paper.get("abstract") or "").strip()
            seen_pages[key] = seen_pages.get(key, 0) + 1
            per_paper[row["paper_id"]] = per_paper.get(row["paper_id"], 0) + 1
            results.append(
                {
                    "paperId": row["paper_id"],
                    "title": paper.get("title") or "",
                    "authors": paper.get("authors") or [],
                    "page": int(row["page"]),
                    "blockId": row["block_id"],
                    "abstract": abstract[:700],
                    "hasAbstract": bool(abstract),
                    "snippet": _snippet(row["text"], query),
                    "score": round(float(row["score"]), 4),
                }
            )
            if len(results) >= limit:
                break
        return results

    def _abstracts(
        self, conn: sqlite3.Connection, paper_ids: list[str]
    ) -> dict[str, str]:
        if not paper_ids:
            return {}
        marks = ",".join("?" for _ in paper_ids)
        rows = conn.execute(
            f"SELECT paper_id, abstract FROM paper_fts WHERE paper_id IN ({marks})",
            tuple(paper_ids),
        ).fetchall()
        return {str(row["paper_id"]): str(row["abstract"] or "") for row in rows}

    def search(
        self, query: str, limit: int = 12, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        text = query.strip()
        if not text:
            return []
        limit = max(1, min(int(limit), 30))
        self.refresh()
        with self._lock, self._connect() as conn:
            # 先多取一些候选，去重后仍能凑满 limit 个不同 paperId。
            results = self._blocks(conn, text, min(limit * 3, 90), project_id) + self._papers(
                conn, text, min(limit, 30), project_id
            )
            results.sort(key=lambda item: item["score"])
            # 同一篇论文可能命中多页/多个段落，对 agent 来说重复条目只会浪费上下文；
            # 每个 paperId 只保留得分最高的一条，需要细节时再 paper_read。
            deduped: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in results:
                paper_id = str(item.get("paperId") or "")
                if paper_id in seen:
                    continue
                seen.add(paper_id)
                deduped.append(item)
            deduped = deduped[:limit]
            abstracts = self._abstracts(conn, [str(item["paperId"]) for item in deduped])
            for item in deduped:
                abstract = str(item.get("abstract") or "").strip()
                if not abstract:
                    item["abstract"] = abstracts.get(str(item["paperId"]), "")
                item["hasAbstract"] = bool(item["abstract"])
            return deduped

    def read_paper(
        self, paper_id: str, page: int | None = None, max_chars: int = 6000
    ) -> dict[str, Any] | None:
        paper = self.library.get(paper_id)
        if not paper:
            return None
        blocks = self.library.read_blocks(paper_id)
        if blocks is None:
            return {"needsIngest": True, "paperId": paper_id, "title": paper.get("title") or ""}

        max_chars = max(500, min(int(max_chars), 30000))
        picked: list[dict[str, Any]] = []
        total = 0
        truncated = False
        for block in blocks:
            if page is not None and int(block.get("page") or 0) != page:
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue
            if total >= max_chars:
                truncated = True
                break
            piece = text[: max_chars - total]
            picked.append(
                {
                    "id": block.get("id") or "",
                    "page": int(block.get("page") or 1),
                    "type": block.get("type") or "text",
                    "text": piece,
                }
            )
            total += len(piece)
            if len(piece) < len(text):
                truncated = True
                break

        return {
            "paperId": paper_id,
            "title": paper.get("title") or "",
            "authors": paper.get("authors") or [],
            "pages": int(paper.get("pages") or 0),
            "blocks": picked,
            "truncated": truncated,
        }
