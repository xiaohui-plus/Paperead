"""直接读取本机 Zotero 数据库的免密钥回退。

Zotero 本地 API 需要用户手动允许外部程序访问；未开启或拒绝连接时，
这里用只读 SQLite 快照直接读取题录和 PDF 附件，保证「无 API Key」也能预览、拉取。
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Any

_REGULAR = ("attachment", "note", "annotation")


def data_dir() -> Path:
    explicit = os.getenv("PAPEREAD_ZOTERO_DATA_DIR") or os.getenv("ZOTERO_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    appdata = os.getenv("APPDATA")
    if appdata:
        profiles = Path(appdata) / "Zotero" / "Zotero" / "Profiles"
        for prefs in profiles.glob("*/prefs.js"):
            try:
                text = prefs.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = re.search(
                r'user_pref\("extensions\.zotero\.dataDir",\s*"([^"]+)"\)', text
            )
            if match:
                return Path(match.group(1).replace("\\\\", "\\"))
    return Path.home() / "Zotero"


def db_path() -> Path:
    return data_dir() / "zotero.sqlite"


def _connect() -> sqlite3.Connection:
    path = db_path()
    if not path.is_file():
        raise FileNotFoundError(f"未找到本机 Zotero 数据库：{path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    return con


def _year(value: str) -> str:
    match = re.search(r"(1[89]\d{2}|20\d{2})", value or "")
    return match.group(1) if match else ""


def _person(row: sqlite3.Row) -> str:
    first = str(row["firstName"] or "").strip()
    last = str(row["lastName"] or "").strip()
    if int(row["fieldMode"] or 0) == 1:
        return last or first
    return " ".join(part for part in (first, last) if part)


def _chunks(values: list[int], size: int = 500) -> list[list[int]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _attachment_path(att_key: str, raw: str) -> Path | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    if raw.startswith("storage:"):
        name = raw.split(":", 1)[1]
        path = data_dir() / "storage" / att_key / name
        return path if path.is_file() else None
    if raw.lower().startswith("file:"):
        parsed = urlparse(raw)
        local = unquote(parsed.path)
        if os.name == "nt":
            local = local.lstrip("/")
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            local = f"//{parsed.netloc}{unquote(parsed.path)}"
        path = Path(local)
        return path if path.is_file() else None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = data_dir() / path
    return path if path.is_file() else None


def _attachments(con: sqlite3.Connection, item_ids: list[int]) -> dict[int, Path]:
    found: dict[int, Path] = {}
    for chunk in _chunks(item_ids):
        marks = ",".join("?" for _ in chunk)
        rows = con.execute(
            f"""
            SELECT ia.parentItemID, a.key AS attKey, ia.path, ia.contentType
            FROM itemAttachments ia
            JOIN items a ON a.itemID = ia.itemID
            WHERE ia.parentItemID IN ({marks})
              AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
            ORDER BY ia.parentItemID, ia.itemID
            """,
            chunk,
        )
        for row in rows:
            parent = int(row["parentItemID"])
            if parent in found:
                continue
            name = str(row["path"] or "").lower()
            ctype = str(row["contentType"] or "").lower()
            if ctype != "application/pdf" and not name.endswith(".pdf"):
                continue
            path = _attachment_path(str(row["attKey"] or ""), str(row["path"] or ""))
            if path is not None:
                found[parent] = path
    return found


def _meta(con: sqlite3.Connection, item_ids: list[int]) -> dict[int, dict[str, str]]:
    values: dict[int, dict[str, str]] = {item_id: {} for item_id in item_ids}
    for chunk in _chunks(item_ids):
        marks = ",".join("?" for _ in chunk)
        rows = con.execute(
            f"""
            SELECT d.itemID, f.fieldName, v.value
            FROM itemData d
            JOIN fields f ON f.fieldID = d.fieldID
            JOIN itemDataValues v ON v.valueID = d.valueID
            WHERE d.itemID IN ({marks})
            """,
            chunk,
        )
        for row in rows:
            values[int(row["itemID"])][str(row["fieldName"])] = str(row["value"] or "")
    return values


def _authors(con: sqlite3.Connection, item_ids: list[int]) -> dict[int, str]:
    names: dict[int, list[tuple[int, str, str]]] = {item_id: [] for item_id in item_ids}
    for chunk in _chunks(item_ids):
        marks = ",".join("?" for _ in chunk)
        rows = con.execute(
            f"""
            SELECT ic.itemID, c.firstName, c.lastName, c.fieldMode, ct.creatorType,
                   ic.orderIndex
            FROM itemCreators ic
            JOIN creators c ON c.creatorID = ic.creatorID
            LEFT JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
            WHERE ic.itemID IN ({marks})
            ORDER BY ic.itemID, ic.orderIndex
            """,
            chunk,
        )
        for row in rows:
            names[int(row["itemID"])].append(
                (int(row["orderIndex"] or 0), str(row["creatorType"] or ""), _person(row))
            )
    result: dict[int, str] = {}
    for item_id, entries in names.items():
        authors = [name for _, kind, name in entries if kind in ("", "author")]
        if not authors:
            authors = [name for _, _, name in entries]
        result[item_id] = ", ".join(authors[:8])
    return result


def list_items(
    *,
    q: str = "",
    limit: int = 50,
    start: int = 0,
    collection: str = "",
    keys: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """读取题录和 PDF 路径；不依赖 Zotero 本地 HTTP API 或 Web API Key。"""
    con = _connect()
    try:
        sql = """
            SELECT i.itemID, i.key, t.typeName, i.dateAdded
            FROM items i
            JOIN itemTypes t ON t.itemTypeID = i.itemTypeID
            WHERE t.typeName NOT IN ('attachment', 'note', 'annotation')
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
        """
        params: list[Any] = []
        collection = (collection or "").strip()
        if collection:
            sql += """
              AND i.itemID IN (
                SELECT ci.itemID FROM collectionItems ci
                JOIN collections c ON c.collectionID = ci.collectionID
                WHERE c.key = ?
              )
            """
            params.append(collection)
        rows = con.execute(sql, params).fetchall()
        wanted = {str(item).strip() for item in (keys or []) if str(item).strip()}
        if wanted:
            rows = [row for row in rows if str(row["key"]) in wanted]

        item_ids = [int(row["itemID"]) for row in rows]
        meta = _meta(con, item_ids)
        authors = _authors(con, item_ids)
        attachments = _attachments(con, item_ids)

        items: list[dict[str, Any]] = []
        for row in rows:
            item_id = int(row["itemID"])
            key = str(row["key"] or "")
            fields = meta.get(item_id, {})
            title = str(fields.get("title") or "").strip()
            if not title:
                title = "(无标题)"
            pdf_path = attachments.get(item_id)
            pdf = (
                {
                    "key": pdf_path.parent.name,
                    "filename": pdf_path.name,
                }
                if pdf_path is not None
                else None
            )
            if not str(fields.get("title") or "").strip() and pdf is None:
                continue
            item = {
                "key": key,
                "title": title,
                "authors": authors.get(item_id, ""),
                "year": _year(str(fields.get("date") or "")) or None,
                "item_type": str(row["typeName"] or ""),
                "publication": str(fields.get("publicationTitle") or fields.get("bookTitle") or ""),
                "doi": str(fields.get("DOI") or ""),
                "url": str(fields.get("url") or ""),
                "date_added": str(row["dateAdded"] or ""),
                "pdf": pdf,
                "imported": False,
                "thumb_url": (
                    "/api/zotero/thumb?key=" + key if pdf is not None else None
                ),
            }
            items.append(item)

        search = (q or "").strip().lower()
        if search:
            items = [
                item
                for item in items
                if search in " ".join(
                    str(item.get(key) or "")
                    for key in ("title", "authors", "publication", "doi")
                ).lower()
            ]
        items.sort(key=lambda item: str(item.get("date_added") or ""), reverse=True)
        total = len(items)
        return items[start : start + limit], total
    finally:
        con.close()


def pdf_path(key: str) -> Path | None:
    con = _connect()
    try:
        row = con.execute(
            "SELECT itemID FROM items WHERE key = ? AND itemID NOT IN (SELECT itemID FROM deletedItems)",
            (key,),
        ).fetchone()
        if not row:
            return None
        return _attachments(con, [int(row["itemID"])]).get(int(row["itemID"]))
    finally:
        con.close()


def status() -> dict[str, Any]:
    path = db_path()
    result: dict[str, Any] = {
        "ok": False,
        "version": "local-db",
        "data_dir": str(path.parent) if path.parent.exists() else "",
        "total": 0,
        "error": "",
    }
    if not path.is_file():
        result["error"] = f"未找到本机 Zotero 数据库：{path}"
        return result
    try:
        con = _connect()
        try:
            result["total"] = int(
                con.execute(
                    """
                    SELECT COUNT(*)
                    FROM items i
                    JOIN itemTypes t ON t.itemTypeID = i.itemTypeID
                    WHERE t.typeName NOT IN ('attachment', 'note', 'annotation')
                      AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                    """
                ).fetchone()[0]
            )
            result["ok"] = True
        finally:
            con.close()
    except Exception as error:
        result["error"] = f"读取本机 Zotero 数据库失败：{error}"
    return result