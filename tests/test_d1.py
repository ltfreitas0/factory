"""D1 shim: sqlite3-surface parity over the Cloudflare D1 HTTP API.

Serves a fake D1 endpoint backed by real sqlite3 so the migration path
(executescript + PRAGMA + ALTER) and row semantics are exercised, not just
unit-mocked. Mirrors db.connect() behavior with D1_* env set.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from factory import db


class _D1Handler(BaseHTTPRequestHandler):
    """In-process D1 stand-in: executes statements against a real sqlite3 DB."""

    backend = None  # set per-server

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        statements = payload if isinstance(payload, list) else [payload]
        results = []
        conn = self.backend["conn"]
        try:
            for stmt in statements:
                sql = stmt.get("sql") or ""
                params = stmt.get("params") or []
                cur = conn.execute(sql, params)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    results.append(
                        {"results": [dict(zip(cols, r)) for r in cur.fetchall()], "success": True}
                    )
                else:
                    results.append({"results": [], "success": True})
            conn.commit()
            ok, status = True, 200
        except Exception as exc:  # surface to client for debugging
            conn.rollback()
            ok, status = False, 500
            results = [{"results": [], "success": False, "error": str(exc)}]
        body = json.dumps({"success": ok, "result": results}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_d1(tmp_path, monkeypatch):
    backend_db = tmp_path / "d1-backend.db"
    conn = sqlite3.connect(backend_db, check_same_thread=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _D1Handler)
    _D1Handler.backend = {"conn": conn}
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]
    monkeypatch.setenv("D1_ACCOUNT_ID", "acct-d1")
    monkeypatch.setenv("D1_DATABASE_ID", "db-1")
    monkeypatch.setenv("D1_API_TOKEN", "tok")
    monkeypatch.setenv("D1_BASE", f"http://127.0.0.1:{port}/client/v4")
    yield conn, server
    server.shutdown()
    conn.close()


def test_connect_runs_full_migration(fake_d1):
    """db.connect() with D1 env must create every table + column migration."""
    backend_conn, _ = fake_d1
    conn = db.connect()
    try:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"projects", "tickets", "documents", "runs", "events", "errors", "feed_log",
            "files", "messages", "users", "connections", "branches", "snapshots",
            "instances", "deployments", "sandboxes", "domains", "llm_providers"} <= tables
    # backend sqlite really received them
    btables = {
        r[0]
        for r in backend_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "users" in btables and "deployments" in btables


def test_row_parity_and_params(fake_d1):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO users (id, google_sub, email, name, created_at) VALUES (?, ?, ?, ?, ?)",
            ("u1", "sub-1", "a@b.c", "Ann", "2026-08-16T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO projects (id, slug, name, repo_path, infra_plugin, created_at, mode) "
            "VALUES (?, ?, ?, ?, 'none', ?, 'live')",
            ("p1", "demo", "Demo", "/tmp/demo", "2026-08-16T00:00:00+00:00"),
        )
        row = conn.execute(
            "SELECT * FROM users WHERE google_sub = ?", ("sub-1",)
        ).fetchone()
        assert row["email"] == "a@b.c"          # str key
        assert row[0] == "u1"                    # int key (sqlite3.Row parity)
        assert dict(row)["name"] == "Ann"        # dict(row)
        assert row.keys() == ["id", "google_sub", "email", "name", "avatar_url",
                              "default_provider_id", "created_at"]
        rows = conn.execute("SELECT slug, mode FROM projects").fetchall()
        assert [r["slug"] for r in rows] == ["demo"]
        assert rows[0]["mode"] == "live"
    finally:
        conn.close()


def test_executescript_and_pragma(fake_d1):
    conn = db.connect()
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS scratch (id INTEGER PRIMARY KEY, v TEXT);"
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scratch)")}
        assert cols == {"id", "v"}
    finally:
        conn.close()


def test_commit_is_noop_and_idempotent(fake_d1):
    conn = db.connect()
    try:
        conn.execute("INSERT INTO branches (id, project_id, name, kind, created_at) "
                     "VALUES ('b1', 'p1', 'main', 'main', '2026-08-16T00:00:00+00:00')")
        conn.commit()  # must not raise
        conn.rollback()
        conn.commit()
        n = conn.execute("SELECT COUNT(*) n FROM branches").fetchone()["n"]
        assert n == 1
    finally:
        conn.close()
