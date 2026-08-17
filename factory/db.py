"""SQLite store. One file, few tables — tickets are the source of truth."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  repo_path TEXT NOT NULL,
  validate_cmd TEXT,
  infra_plugin TEXT NOT NULL DEFAULT 'none',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  state TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'build',
  source TEXT NOT NULL DEFAULT 'human',
  parent_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  version INTEGER NOT NULL,
  body TEXT NOT NULL,
  author TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  ticket_id TEXT,
  project_id TEXT,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  session_id TEXT,
  stdout TEXT,
  stderr TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  tokens INTEGER,
  FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT,
  run_id TEXT,
  type TEXT NOT NULL,
  payload TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  detail TEXT,
  ticket_id TEXT,
  run_id TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  ticket_id TEXT,
  state TEXT,
  title TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  google_sub TEXT UNIQUE,
  email TEXT,
  name TEXT,
  avatar_url TEXT,
  default_provider_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  external_id TEXT,
  scopes TEXT,
  token_ref TEXT,
  cf_account_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'feature',
  head_sha TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  branch_id TEXT,
  sha TEXT NOT NULL,
  message TEXT,
  created_by TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  production INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'sandbox',
  account TEXT NOT NULL DEFAULT 'platform',
  url TEXT,
  branch TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS deployments (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  branch TEXT,
  sha TEXT,
  status TEXT NOT NULL DEFAULT 'provisioning',
  infra_ref TEXT,
  url TEXT,
  build_log_ref TEXT,
  deployed_by TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS sandboxes (
  project_id TEXT PRIMARY KEY,
  sandbox_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'stopped',
  instance_type TEXT NOT NULL DEFAULT 'lite',
  port INTEGER,
  preview_url TEXT,
  last_synced_sha TEXT,
  seen_at TEXT
);
CREATE TABLE IF NOT EXISTS domains (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  hostname TEXT NOT NULL,
  target_instance_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  cf_record_ref TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (project_id, hostname)
);
CREATE TABLE IF NOT EXISTS llm_providers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'opencode',
  base_url TEXT,
  model TEXT,
  api_key_ref TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.environ.get("FACTORY_DB", "data/factory.db"))


def connect(path: Path | None = None):
    """Local sqlite3 (default) or D1 when D1_* env is set and no path given."""
    from factory import d1

    if path is None and d1.configured():
        conn = d1.connect()
    else:
        p = path or db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN kind TEXT NOT NULL DEFAULT 'build'")
    if "source" not in cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN source TEXT NOT NULL DEFAULT 'human'")
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "usage_json" not in rcols:
        conn.execute("ALTER TABLE runs ADD COLUMN usage_json TEXT")
    # runs: ticket_id became nullable and project_id was added (live-mode chat).
    # sqlite can't drop NOT NULL in place; rebuild when the old shape is found.
    if "project_id" not in rcols or _col_notnull(conn, "runs", "ticket_id"):
        conn.execute(
            """CREATE TABLE runs_new (
                 id TEXT PRIMARY KEY,
                 ticket_id TEXT,
                 project_id TEXT,
                 stage TEXT NOT NULL,
                 status TEXT NOT NULL,
                 session_id TEXT,
                 stdout TEXT,
                 stderr TEXT,
                 started_at TEXT NOT NULL,
                 finished_at TEXT,
                 tokens INTEGER,
                 usage_json TEXT
               )"""
        )
        conn.execute(
            """INSERT INTO runs_new (id, ticket_id, project_id, stage, status, session_id,
                 stdout, stderr, started_at, finished_at, tokens, usage_json)
               SELECT id, ticket_id, NULL, stage, status, session_id,
                 stdout, stderr, started_at, finished_at, tokens, usage_json FROM runs"""
        )
        conn.execute("DROP TABLE runs")
        conn.execute("ALTER TABLE runs_new RENAME TO runs")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    if "workflow" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN workflow TEXT")
    if "active_instance" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN active_instance TEXT DEFAULT 'dev'")
    if "name" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN name TEXT")
        conn.execute("UPDATE projects SET name = slug WHERE name IS NULL")
    if "git_remote" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN git_remote TEXT")
    if "owner_id" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN owner_id TEXT")
    if "mode" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN mode TEXT NOT NULL DEFAULT 'live'")
    if "quality_gates" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN quality_gates INTEGER NOT NULL DEFAULT 0")
    if "provider_id" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN provider_id TEXT")
    if "template_id" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN template_id TEXT")
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
    if "stage" not in tcols:
        conn.execute("ALTER TABLE tickets ADD COLUMN stage TEXT")
    if "status" not in tcols:
        conn.execute("ALTER TABLE tickets ADD COLUMN status TEXT")
    from factory import files as files_mod
    from factory import messages as messages_mod
    from factory import project as project_mod

    files_mod.ensure_schema(conn)
    messages_mod.ensure_schema(conn)
    _backfill_ticket_axes(conn)
    for row in conn.execute("SELECT id, workflow FROM projects"):
        if not row["workflow"]:
            conn.execute(
                "UPDATE projects SET workflow = ? WHERE id = ?",
                (__import__("json").dumps(project_mod.DEFAULT_WORKFLOW), row["id"]),
            )
    conn.commit()
    return conn


def _col_notnull(conn, table: str, column: str) -> bool:
    for r in conn.execute(f"PRAGMA table_info({table})"):
        if r[1] == column:
            return bool(r[3])
    return False


def _backfill_ticket_axes(conn: sqlite3.Connection) -> None:
    from factory.project import infer_stage_status

    for row in conn.execute("SELECT id, state, stage, status FROM tickets"):
        if row["stage"] and row["status"]:
            continue
        stage, status = infer_stage_status(row["state"])
        conn.execute(
            "UPDATE tickets SET stage = ?, status = ? WHERE id = ?",
            (stage, status, row["id"]),
        )


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else dict(row)


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
