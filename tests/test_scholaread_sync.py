from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest

from server import scholaread
from server.store import LibraryStore

READING_SQL = """
CREATE TABLE reading_list (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id TEXT, type TEXT, name TEXT, title TEXT, authors TEXT,
    url TEXT, link TEXT, hash TEXT, content_type TEXT, paper_id TEXT,
    share_id TEXT, blob_upload_id TEXT, total_num INTEGER, read_mode TEXT,
    read_progress INTEGER, read_point TEXT, read_state TEXT, read_at TEXT,
    create_at TEXT, update_at TEXT, tags TEXT, file_path TEXT, size TEXT,
    cache_path TEXT, picture TEXT, doi TEXT, arxiv_id TEXT, preset INTEGER,
    doc_type TEXT
);
"""

EVENT_SQL = """
CREATE TABLE reading_list_events (
    client_event_id TEXT PRIMARY KEY, event_id TEXT, event_at TEXT,
    device_id TEXT, op TEXT, op_at TEXT, data_id TEXT,
    data_local_row_id INTEGER, data_fields TEXT, data TEXT, sign TEXT,
    synced INTEGER, sync_result_code INTEGER, sync_result_message TEXT
);
"""

NOTES_SQL = """
CREATE TABLE notes (
    note_id TEXT PRIMARY KEY, paper_row_id INTEGER, reading_id TEXT,
    share_id TEXT, type TEXT, text TEXT, lines TEXT, note_content TEXT,
    create_at TEXT, update_at TEXT, mark_style TEXT
);
"""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pdf_bytes() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def _make_scholaread(
    root: Path,
    rows: list[dict],
    *,
    pdfs: bool = True,
) -> Path:
    """建一个最小的学术阅读数据目录，返回 data_dir。"""
    (root / "files" / "pdf").mkdir(parents=True, exist_ok=True)
    db = root / "scholaread.sqlite"
    con = sqlite3.connect(db)
    try:
        con.executescript(READING_SQL + EVENT_SQL + NOTES_SQL)
        for row in rows:
            keys = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            con.execute(
                f"insert into reading_list ({keys}) values ({marks})",
                tuple(row.values()),
            )
            if pdfs and row.get("cache_path"):
                (root / "files" / "pdf" / row["cache_path"]).write_bytes(_pdf_bytes())
        con.commit()
    finally:
        con.close()
    return root


def _remote_row(reading_id: str = "r1", **overrides) -> dict:
    now = time.time()
    row = {
        "reading_id": reading_id,
        "type": "blob_upload",
        "title": "Structural Information Enhanced Graph Representation",
        "authors": json.dumps([{"name": "Lei Shi"}, {"name": "Bin Hu"}]),
        "doi": "10.1609/aaai.v38i13.29417",
        "total_num": 20,
        "read_progress": 2,
        "read_point": "0.05000",
        "read_state": "doing",
        "read_at": _iso(now - 3600),
        "update_at": _iso(now - 3600),
        "cache_path": f"{reading_id}.pdf",
    }
    row.update(overrides)
    return row


def _local_row(paper_id: str, **overrides) -> dict:
    row = {
        "id": paper_id,
        "title": "Local paper",
        "authors": "",
        "filename": f"{paper_id}.pdf",
        "pages": 3,
        "size": 100,
        "created_at": 1,
        "status": "ready",
        "error": "",
        "translated": 0,
        "blocks": 0,
    }
    row.update(overrides)
    return row


def _seed(store: LibraryStore, rows: list[dict]) -> None:
    store.index.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _db_rows(data_dir: Path) -> list[dict]:
    con = sqlite3.connect(data_dir / "scholaread.sqlite")
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute("select * from reading_list")]
    finally:
        con.close()


def _events(data_dir: Path) -> list[dict]:
    con = sqlite3.connect(data_dir / "scholaread.sqlite")
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(row) for row in con.execute("select * from reading_list_events")
        ]
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _not_running(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scholaread, "_is_running", lambda: False)


def test_status_missing_dir(tmp_path: Path):
    status = scholaread._status(tmp_path / "missing")
    assert status["ok"] is False
    assert status["exists"] is False
    assert "未找到" in status["error"]


def test_status_counts(tmp_path: Path):
    data_dir = _make_scholaread(tmp_path / "sr", [_remote_row()])
    status = scholaread._status(data_dir)
    assert status["ok"] is True
    assert status["schema_ok"] is True
    assert status["total"] == 1
    assert status["with_pdf"] == 1
    assert status["running"] is False


def test_pull_imports_and_dedupes(tmp_path: Path):
    data_dir = _make_scholaread(tmp_path / "sr", [_remote_row()])
    store = LibraryStore(tmp_path / "lib")

    result, new_ids = scholaread._run_sync(store, str(data_dir), True, False, 50)
    assert result["ok"] is True
    assert result["pull"]["imported"] == 1
    assert len(new_ids) == 1

    papers = store.list()
    assert len(papers) == 1
    assert papers[0]["source_id"] == "scholaread:r1"
    assert papers[0]["doi"] == "10.1609/aaai.v38i13.29417"
    assert papers[0]["scholaread_id"] == "r1"
    assert papers[0]["read_pct"] == 5.0

    again, _ = scholaread._run_sync(store, str(data_dir), True, False, 50)
    assert again["pull"]["imported"] == 0
    assert len(store.list()) == 1


def test_pull_skips_missing_pdf(tmp_path: Path):
    data_dir = _make_scholaread(
        tmp_path / "sr", [_remote_row(cache_path="not-downloaded.pdf")], pdfs=False
    )
    store = LibraryStore(tmp_path / "lib")
    result, _ = scholaread._run_sync(store, str(data_dir), True, False, 50)
    assert result["pull"]["skipped_no_pdf"] == 1
    assert store.list() == []


def test_pull_rejects_path_traversal(tmp_path: Path):
    (tmp_path / "secret.pdf").write_bytes(_pdf_bytes())
    data_dir = _make_scholaread(
        tmp_path / "sr", [_remote_row(cache_path="../secret.pdf")]
    )
    store = LibraryStore(tmp_path / "lib")
    result, _ = scholaread._run_sync(store, str(data_dir), True, False, 50)
    assert result["pull"]["imported"] == 0
    assert result["pull"]["skipped_no_pdf"] == 1
    assert store.list() == []


def test_pull_updates_newer_remote_progress(tmp_path: Path):
    now = time.time()
    data_dir = _make_scholaread(
        tmp_path / "sr",
        [
            _remote_row(
                read_point="0.50000",
                read_progress=10,
                read_at=_iso(now),
                update_at=_iso(now),
            )
        ],
    )
    store = LibraryStore(tmp_path / "lib")
    _seed(
        store,
        [
            _local_row(
                "p1",
                source="scholaread",
                source_id="scholaread:r1",
                scholaread_id="r1",
                read_pct=10.0,
                read_at=now - 7200,
            )
        ],
    )
    result, _ = scholaread._run_sync(store, str(data_dir), True, False, 50)
    assert result["pull"]["progress_updated"] == 1
    paper = store.get("p1")
    assert paper is not None
    assert paper["read_pct"] == 50.0
    assert paper["read_page"] == 10


def test_push_refuses_while_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = _make_scholaread(tmp_path / "sr", [_remote_row()])
    store = LibraryStore(tmp_path / "lib")
    now = time.time()
    _seed(
        store,
        [
            _local_row(
                "p1",
                scholaread_id="r1",
                read_pct=60.0,
                read_at=now,
            )
        ],
    )
    monkeypatch.setattr(scholaread, "_is_running", lambda: True)
    result = scholaread._push(store, data_dir)
    assert result["updated"] == 0
    assert "退出" in result["error"]
    assert _db_rows(data_dir)[0]["read_point"] == "0.05000"


def test_push_writes_progress_and_event(tmp_path: Path):
    data_dir = _make_scholaread(tmp_path / "sr", [_remote_row()])
    store = LibraryStore(tmp_path / "lib")
    now = time.time()
    _seed(
        store,
        [
            _local_row(
                "p1",
                scholaread_id="r1",
                read_pct=60.0,
                read_at=now,
            )
        ],
    )
    result = scholaread._push(store, data_dir)
    assert result["error"] == ""
    assert result["updated"] == 1
    assert Path(result["backup"]).is_file()

    remote = _db_rows(data_dir)[0]
    assert remote["read_point"] == "0.60000"
    assert remote["read_progress"] == 12
    assert remote["read_state"] == "doing"

    events = _events(data_dir)
    assert len(events) == 1
    assert events[0]["op"] == "update"
    assert events[0]["data_id"] == "r1"
    assert events[0]["synced"] == 0
    assert json.loads(events[0]["data"])["read_point"] == "0.60000"


def test_push_skips_when_remote_is_newer(tmp_path: Path):
    now = time.time()
    data_dir = _make_scholaread(
        tmp_path / "sr",
        [
            _remote_row(
                read_point="0.80000",
                read_at=_iso(now + 60),
                update_at=_iso(now + 60),
            )
        ],
    )
    store = LibraryStore(tmp_path / "lib")
    _seed(
        store,
        [
            _local_row(
                "p1",
                scholaread_id="r1",
                read_pct=60.0,
                read_at=now,
            )
        ],
    )
    result = scholaread._push(store, data_dir)
    assert result["updated"] == 0
    assert _db_rows(data_dir)[0]["read_point"] == "0.80000"
    assert _events(data_dir) == []
