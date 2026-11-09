"""本地文献库全文检索（SQLite FTS5 trigram）。

索引放在 `data/research/search.db`，按 library.json 与 blocks 文件的
修改时间做增量刷新。

检索分三步：
1. 论文路（标题/摘要/标签加权 BM25）与段落路（全文 BM25）各自召回；
2. 用 RRF 在论文级别融合两路排名，避免把两张表的 BM25 原始分直接混排；
3. 用“概念覆盖度 + 字段命中 + 时效 + 证据完整度”做可解释重排。

结果直接带最佳页码与命中页列表，让模型在检索阶段就能拿到可引用位置，
而不是为了找页码再逐篇 paper_read。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from server.store import LibraryStore

MAX_SNIPPET = 260
INDEX_VERSION = 3  # 摘要摘录/字段权重/覆盖度等索引语义变化时递增，触发重建
CJK_RE = re.compile(r"[\u3400-\u9fff]")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-+]*")
RRF_K = 60
PAPER_POOL = 60
BLOCK_POOL = 120
MAX_PAGES = 3

# 这些词几乎出现在每篇英文论文里，参与覆盖度只会放大噪声。
STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "these",
    "those", "are", "was", "were", "has", "have", "had", "not", "but",
    "can", "may", "might", "use", "used", "using", "via", "per", "its",
    "their", "our", "all", "any", "how", "why", "what", "when", "where",
    "which", "who", "will", "would", "should", "could", "than", "then",
    "also", "more", "most", "such", "between", "among", "within", "without",
    "based", "study", "studies", "paper", "papers", "review", "results",
}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def _fts_expr(text: str) -> str:
    """把查询转成 FTS5 表达式。

    英文多词查询按词 OR（bm25 会把命中更多词的结果排前），避免整句精确短语
    在长查询下几乎总是 0 命中；中文与单词查询保持精确短语。
    """
    cleaned = text.strip()
    if _has_cjk(cleaned) or " " not in cleaned:
        return '"' + cleaned.replace('"', '""') + '"'
    tokens = [token for token in re.split(r"\s+", cleaned) if len(token) >= 3]
    if not tokens:
        return '"' + cleaned.replace('"', '""') + '"'
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def _fts_query(text: str, column: str | None = None) -> str:
    expr = _fts_expr(text)
    return f"{column} : ({expr})" if column else expr


def _concepts(text: str) -> list[str]:
    """查询概念：英文按词切分，中文整句作为一个概念。"""
    cleaned = text.strip()
    if not cleaned:
        return []
    if _has_cjk(cleaned):
        return [cleaned.lower()]
    seen: list[str] = []
    for token in TOKEN_RE.findall(cleaned):
        low = token.lower()
        if len(low) < 3 or low in STOPWORDS:
            continue
        if low not in seen:
            seen.append(low)
    return seen or [cleaned.lower()]


def _hit(concept: str, haystack: str) -> bool:
    """概念是否命中；英文做词边界匹配，兼容连字符。"""
    if not haystack:
        return False
    if _has_cjk(concept):
        return concept in haystack
    for variant in {concept, concept.replace("-", " "), concept.replace("-", "")}:
        if re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", haystack):
            return True
    return False


def _matched(concepts: list[str], *fields: str) -> set[str]:
    hay = " ".join(field.lower() for field in fields if field)
    return {concept for concept in concepts if _hit(concept, hay)}


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
    if hit < 0 and " " in needle and not _has_cjk(needle):
        # 多词 OR 查询通常找不到整句，退而定位最长的那个词。
        longest = max(
            (token for token in needle.split() if len(token) >= 3),
            key=len,
            default="",
        )
        hit = compact.lower().find(longest.lower()) if longest else -1
    if hit < 0:
        return compact[:MAX_SNIPPET] + "…"
    start = max(0, hit - 70)
    end = min(len(compact), start + MAX_SNIPPET)
    return ("…" if start else "") + compact[start:end] + ("…" if end < len(compact) else "")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _recency(year: Any) -> float:
    try:
        value = int(year)
    except (TypeError, ValueError):
        return 0.3
    return _clamp((value - 2010) / 16.0)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 3)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 3)


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
        if cols and "tags" not in cols:
            # 索引是可重建的缓存；加字段时直接重建，省掉迁移脚本。
            conn.executescript("DROP TABLE IF EXISTS paper_fts; DROP TABLE IF EXISTS block_fts;")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5(
              paper_id UNINDEXED, project_id UNINDEXED, title, authors, abstract, tags,
              tokenize='trigram'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(
              paper_id UNINDEXED, project_id UNINDEXED, block_id UNINDEXED, page UNINDEXED,
              block_type, text, tokenize='trigram'
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
                tags = paper.get("tags") or []
                if isinstance(tags, str):
                    tags = [tags]
                conn.execute(
                    "INSERT INTO paper_fts (paper_id, project_id, title, authors, abstract, tags)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        paper["id"],
                        project_id,
                        paper.get("title") or "",
                        " ".join(paper.get("authors") or []),
                        abstract,
                        " ".join(str(tag) for tag in tags),
                    ),
                )
                for block in blocks:
                    text = (block.get("text") or "").strip()
                    if not text:
                        continue
                    conn.execute(
                        "INSERT INTO block_fts (paper_id, project_id, block_id, page, block_type, text)"
                        " VALUES (?,?,?,?,?,?)",
                        (
                            paper["id"],
                            project_id,
                            block.get("id") or "",
                            int(block.get("page") or 1),
                            str(block.get("type") or "text"),
                            text,
                        ),
                    )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('stamp', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (stamp,),
            )
            conn.commit()

    # ── 召回 ────────────────────────────────────────────

    @staticmethod
    def _scope(project_id: str | None) -> tuple[str, tuple[str, ...]]:
        """全局文献始终可见；传了项目 id 时再加上该项目的论文。"""
        if project_id:
            return "(project_id = '' OR project_id = ?)", (project_id,)
        return "project_id = ''", ()

    def _paper_rows(
        self,
        conn: sqlite3.Connection,
        query: str,
        project_id: str | None,
        mode: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        where, params = self._scope(project_id)
        if len(query) >= 3:
            column = "title" if mode == "title" else None
            rows = conn.execute(
                "SELECT paper_id, title, authors, abstract, tags,"
                " bm25(paper_fts, 0.0, 0.0, 4.0, 0.5, 1.5, 1.0) AS score "
                f"FROM paper_fts WHERE paper_fts MATCH ? AND {where} "
                "ORDER BY score LIMIT ?",
                (_fts_query(query, column), *params, limit),
            ).fetchall()
            # 小语料下命中唯一文档时 bm25 会退化成 0；这里不再按分数过滤，
            # 噪声交给后面的概念覆盖度判断。
            return list(rows)
        like = f"%{_like_escape(query)}%"
        return conn.execute(
            "SELECT paper_id, title, authors, abstract, tags, 0 AS score FROM paper_fts "
            f"WHERE {where} AND (title LIKE ? ESCAPE '\\' OR abstract LIKE ? ESCAPE '\\'"
            " OR tags LIKE ? ESCAPE '\\') LIMIT ?",
            (*params, like, like, like, limit),
        ).fetchall()

    def _block_rows(
        self,
        conn: sqlite3.Connection,
        query: str,
        project_id: str | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        where, params = self._scope(project_id)
        if len(query) >= 3:
            rows = conn.execute(
                "SELECT paper_id, block_id, page, block_type, text,"
                " bm25(block_fts, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0) AS score "
                f"FROM block_fts WHERE block_fts MATCH ? AND {where} "
                "ORDER BY score LIMIT ?",
                (_fts_query(query), *params, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT paper_id, block_id, page, block_type, text, 0 AS score FROM block_fts "
                f"WHERE {where} AND text LIKE ? ESCAPE '\\' LIMIT ?",
                (*params, f"%{_like_escape(query)}%", limit),
            ).fetchall()
        # 参考文献块几乎不提供可引用证据，还特别容易命中作者名/期刊名。
        return [row for row in rows if str(row["block_type"] or "") != "reference"]

    # ── 重排 ────────────────────────────────────────────

    def _rerank(
        self,
        *,
        query: str,
        concepts: list[str],
        papers: dict[str, dict[str, Any]],
        paper_rows: list[sqlite3.Row],
        block_rows: list[sqlite3.Row],
        mode: str,
        min_coverage: float,
        limit: int,
    ) -> dict[str, Any]:
        candidates: dict[str, dict[str, Any]] = {}

        for rank, row in enumerate(paper_rows):
            paper_id = str(row["paper_id"])
            item = candidates.setdefault(paper_id, {"pages": set(), "rrf": 0.0})
            item["paper"] = row
            item["paperRank"] = rank
            item["rrf"] += 1.0 / (RRF_K + rank + 1)

        block_groups: dict[str, dict[str, Any]] = {}
        for row in block_rows:
            paper_id = str(row["paper_id"])
            item = block_groups.get(paper_id)
            score = float(row["score"])
            if item is None or score < item["score"]:
                block_groups[paper_id] = {"row": row, "score": score}
            else:
                pass
            candidates.setdefault(paper_id, {"pages": set(), "rrf": 0.0})["pages"].add(
                int(row["page"])
            )

        for rank, (paper_id, group) in enumerate(
            sorted(block_groups.items(), key=lambda pair: pair[1]["score"])
        ):
            item = candidates.setdefault(paper_id, {"pages": set(), "rrf": 0.0})
            item["block"] = group["row"]
            item["blockRank"] = rank
            item["rrf"] += 1.0 / (RRF_K + rank + 1)

        if not candidates:
            return {
                "results": [],
                "stats": {"candidates": 0, "kept": 0, "medianCoverage": 0.0},
                "notice": "没有找到相关段落；本地论文以英文为主，建议改用更短的英文关键词。",
            }

        max_rrf = max(item["rrf"] for item in candidates.values()) or 1.0
        total = len(concepts)
        scored: list[dict[str, Any]] = []

        for paper_id, item in candidates.items():
            meta = papers.get(paper_id) or {}
            paper_row = item.get("paper")
            title = str((paper_row["title"] if paper_row else meta.get("title")) or "")
            abstract = str(
                (paper_row["abstract"] if paper_row else meta.get("abstract")) or ""
            ).strip()
            authors = str((paper_row["authors"] if paper_row else "") or "")
            tags = str((paper_row["tags"] if paper_row else "") or "")
            block_row = item.get("block")
            block_text = str(block_row["text"]) if block_row else ""

            matched_title = _matched(concepts, title)
            matched_abstract = _matched(concepts, abstract)
            if mode == "title":
                matched_all = set(matched_title)
                matched_passage: set[str] = set()
            else:
                matched_passage = _matched(concepts, block_text)
                matched_all = matched_title | matched_abstract | matched_passage
            matched_tags = _matched(concepts, tags)
            coverage = len(matched_all) / total if total else 0.0

            if coverage < min_coverage:
                continue

            title_hay = re.sub(r"\s+", " ", title).strip().lower()
            phrase = re.sub(r"\s+", " ", query).strip().lower()
            title_score = 1.0 if phrase and phrase in title_hay else (
                len(matched_title) / total if total else 0.0
            )
            abstract_score = len(matched_abstract) / total if total else 0.0
            passage_score = len(matched_passage) / total if total else 0.0
            tag_score = len(matched_tags) / total if total else 0.0
            has_fulltext = self.library.blocks_path(paper_id).exists()
            quality = 1.0 if has_fulltext else (0.55 if abstract else 0.2)

            score = (
                0.38 * coverage
                + 0.20 * title_score
                + 0.14 * abstract_score
                + 0.14 * passage_score
                + 0.06 * tag_score
                + 0.04 * _recency(meta.get("year"))
                + 0.04 * quality
            )
            score = round(_clamp(score) * (0.9 + 0.1 * (item["rrf"] / max_rrf)), 4)

            pages = sorted(item["pages"] or [])[:MAX_PAGES]
            page = int(block_row["page"]) if block_row else (pages[0] if pages else None)
            snippet_source = block_text or abstract or title
            missing = [concept for concept in concepts if concept not in matched_all]

            reason: list[str] = []
            if matched_title:
                reason.append(f"标题命中 {len(matched_title)}/{total} 个概念")
            if matched_abstract and not matched_title:
                reason.append(f"摘要命中 {len(matched_abstract)}/{total} 个概念")
            if matched_passage:
                reason.append(f"p{page} 段落命中 {len(matched_passage)}/{total} 个概念")
            if missing:
                reason.append("缺 " + "、".join(missing[:3]))

            scored.append(
                {
                    "paperId": paper_id,
                    "title": title,
                    "authors": meta.get("authors")
                    or [part for part in authors.split(" ") if part],
                    "year": meta.get("year"),
                    "venue": meta.get("venue") or "",
                    "page": page,
                    "pages": pages,
                    "blockId": block_row["block_id"] if block_row else None,
                    "snippet": _snippet(snippet_source, query),
                    "abstract": abstract[:700],
                    "hasAbstract": bool(abstract),
                    "hasFullText": has_fulltext,
                    "coverage": round(coverage, 3),
                    "matched": sorted(matched_all),
                    "missing": missing,
                    "score": round(float(score) + 0.0, 4),
                    "why": "；".join(reason) or "关键词命中",
                }
            )

        scored.sort(key=lambda entry: entry["score"], reverse=True)
        kept = scored
        if scored:
            cutoff = scored[0]["score"] * 0.55
            kept = [entry for entry in scored if entry["score"] >= cutoff][:limit]
            if not kept:
                kept = scored[:1]

        notice: str | None = None
        if len(kept) < 3:
            notice = "命中较少，建议换用更短的英文关键词，或拆分概念后再检索。"
        return {
            "results": kept,
            "stats": {
                "candidates": len(candidates),
                "kept": len(kept),
                "medianCoverage": _median([entry["coverage"] for entry in kept]),
            },
            "notice": notice,
        }

    # ── 查询 ────────────────────────────────────────────

    def search_page(
        self,
        query: str,
        limit: int = 12,
        project_id: str | None = None,
        mode: str = "topic",
        min_coverage: float | None = None,
    ) -> dict[str, Any]:
        text = query.strip()
        if not text:
            return {
                "query": "",
                "results": [],
                "stats": {"candidates": 0, "kept": 0, "medianCoverage": 0.0},
                "notice": "查询为空。",
            }
        limit = max(1, min(int(limit), 30))
        mode = "title" if mode == "title" else "topic"
        concepts = _concepts(text)
        if min_coverage is None:
            # 至少命中 60% 的概念（向上取整）：两个概念的查询要求两个都命中，
            # 否则“dual-target CAR-T”这类查询会把只命中一个词的结果全带进来。
            needed = 1 if len(concepts) <= 1 else (len(concepts) * 3 + 4) // 5
            min_coverage = needed / len(concepts)
        min_coverage = max(0.0, min(float(min_coverage), 1.0))

        self.refresh()
        papers = {str(row["id"]): row for row in self.library.list(include_project=True)}
        with self._lock, self._connect() as conn:
            paper_rows = self._paper_rows(
                conn, text, project_id, mode, min(PAPER_POOL, max(limit * 4, 40))
            )
            block_rows = (
                []
                if mode == "title"
                else self._block_rows(conn, text, project_id, BLOCK_POOL)
            )
            payload = self._rerank(
                query=text,
                concepts=concepts,
                papers=papers,
                paper_rows=paper_rows,
                block_rows=block_rows,
                mode=mode,
                min_coverage=min_coverage,
                limit=limit,
            )
            payload["query"] = text
            payload["mode"] = mode
            return payload

    def search(
        self,
        query: str,
        limit: int = 12,
        project_id: str | None = None,
        mode: str = "topic",
        min_coverage: float | None = None,
    ) -> list[dict[str, Any]]:
        return self.search_page(
            query,
            limit=limit,
            project_id=project_id,
            mode=mode,
            min_coverage=min_coverage,
        )["results"]

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
