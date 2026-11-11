"""研究库 SQLite：连接、建表与版本。

库文件与项目工作目录都在 ``<data>/research/`` 下：
    research.db
    projects/<projectId>/files/
    projects/<projectId>/artifacts/
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'research' CHECK (kind IN ('research','review')),
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  research_question TEXT NOT NULL DEFAULT '',
  instructions TEXT NOT NULL DEFAULT '',
  workspace TEXT NOT NULL,
  root_path TEXT NOT NULL DEFAULT '',
  default_model TEXT NOT NULL DEFAULT 'deepseek-v4-flash',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_projects_kind_updated
  ON projects(kind, archived, updated_at DESC);

CREATE TABLE IF NOT EXISTS project_items (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  parent_id TEXT REFERENCES project_items(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('paper','file','folder','note')),
  ref_id TEXT,
  path TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}',
  added_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_items_parent
  ON project_items(project_id, parent_id);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '新会话',
  model TEXT NOT NULL,
  effort TEXT NOT NULL DEFAULT 'high',
  status TEXT NOT NULL DEFAULT 'idle',
  agent_seq INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_turn_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_project_updated
  ON sessions(project_id, updated_at DESC);

-- 会话事件：Python 侧自增 seq 是产品真相，agent 的 seq 单独记录用于续订。
CREATE TABLE IF NOT EXISTS session_events (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  agent_seq INTEGER,
  at INTEGER NOT NULL,
  type TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (session_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_events_agent
  ON session_events(session_id, agent_seq, at);

CREATE TABLE IF NOT EXISTS session_sources (
  session_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, source_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  session_id TEXT,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS search_cache (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """每次操作一条短连接：WAL + 外键 + 忙等，避免跨线程复用。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            columns = {
                item["name"] for item in conn.execute("PRAGMA table_info(sessions)")
            }
            if "agent_seq" not in columns:
                # v1 → v2：记录已消费到的 agent 游标（当前事件表也用它续订）。
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN agent_seq INTEGER NOT NULL DEFAULT 0"
                )
            project_columns = {
                item["name"] for item in conn.execute("PRAGMA table_info(projects)")
            }
            if "root_path" not in project_columns:
                # v2 → v3：项目可以绑定电脑上的任意文件夹作为工作目录。
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN root_path TEXT NOT NULL DEFAULT ''"
                )
            conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
