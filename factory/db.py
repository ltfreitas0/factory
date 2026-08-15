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
  ticket_id TEXT NOT NULL,
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
"""


def db_path() -> Path:
    return Path(os.environ.get("FACTORY_DB", "data/factory.db"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else dict(row)


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
