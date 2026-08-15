"""Ticket/document/run writes. The control plane's only mutation path."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from factory import feed, obs, sm
from factory.db import row_dict, rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def add_event(
    conn: sqlite3.Connection,
    typ: str,
    *,
    ticket_id: str | None = None,
    run_id: str | None = None,
    payload: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (ticket_id, run_id, type, payload, at) VALUES (?, ?, ?, ?, ?)",
        (ticket_id, run_id, typ, json.dumps(payload or {}), _now()),
    )


def ensure_project(
    conn: sqlite3.Connection,
    slug: str,
    repo_path: str,
    validate_cmd: str = "true",
) -> dict:
    row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row:
        conn.execute(
            "UPDATE projects SET repo_path = ?, validate_cmd = ? WHERE slug = ?",
            (repo_path, validate_cmd, slug),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone())
    pid = _id("prj")
    conn.execute(
        """INSERT INTO projects (id, slug, repo_path, validate_cmd, infra_plugin, created_at)
           VALUES (?, ?, ?, ?, 'none', ?)""",
        (pid, slug, repo_path, validate_cmd, _now()),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone())


def ensure_playground(conn: sqlite3.Connection, repo_path: str) -> dict:
    return ensure_project(conn, "playground", repo_path, "python3 -m pytest -q")


def active_ticket(conn: sqlite3.Connection) -> dict | None:
    return row_dict(
        conn.execute(
            """SELECT * FROM tickets
               WHERE state NOT IN ('done', 'failed', 'needs_human', 'inbox')
               ORDER BY updated_at DESC LIMIT 1"""
        ).fetchone()
    )


def create_ticket(conn: sqlite3.Connection, *, project_id: str, title: str, body: str) -> dict:
    tid = _id("tkt")
    now = _now()
    conn.execute(
        """INSERT INTO tickets (id, project_id, state, title, body, created_at, updated_at)
           VALUES (?, ?, 'inbox', ?, ?, ?, ?)""",
        (tid, project_id, title, body, now, now),
    )
    add_event(conn, "ticket_created", ticket_id=tid, payload={"title": title})
    conn.commit()
    return get_ticket(conn, tid)


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> dict | None:
    t = row_dict(conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone())
    if not t:
        return None
    t["documents"] = rows(
        conn.execute(
            "SELECT * FROM documents WHERE ticket_id = ? ORDER BY kind, version DESC",
            (ticket_id,),
        )
    )
    t["runs"] = rows(
        conn.execute(
            "SELECT * FROM runs WHERE ticket_id = ? ORDER BY started_at DESC",
            (ticket_id,),
        )
    )
    return t


def list_tickets(conn: sqlite3.Connection) -> list[dict]:
    return rows(conn.execute("SELECT * FROM tickets ORDER BY created_at DESC"))


def latest_doc(conn: sqlite3.Connection, ticket_id: str, kind: str) -> dict | None:
    return row_dict(
        conn.execute(
            """SELECT * FROM documents WHERE ticket_id = ? AND kind = ?
               ORDER BY version DESC LIMIT 1""",
            (ticket_id, kind),
        ).fetchone()
    )


def put_doc(
    conn: sqlite3.Connection, ticket_id: str, kind: str, body: str, author: str
) -> dict:
    prev = latest_doc(conn, ticket_id, kind)
    version = 1 if prev is None else int(prev["version"]) + 1
    did = _id("doc")
    conn.execute(
        """INSERT INTO documents (id, ticket_id, kind, version, body, author, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (did, ticket_id, kind, version, body, author, _now()),
    )
    add_event(
        conn,
        "document_written",
        ticket_id=ticket_id,
        payload={"kind": kind, "version": version, "author": author},
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone())


def transition(
    conn: sqlite3.Connection, ticket_id: str, dst: str, actor: str
) -> dict:
    t = get_ticket(conn, ticket_id)
    if not t:
        raise KeyError(ticket_id)
    src = t["state"]
    sm.transition(src, dst, actor)
    now = _now()
    conn.execute(
        "UPDATE tickets SET state = ?, updated_at = ? WHERE id = ?",
        (dst, now, ticket_id),
    )
    add_event(
        conn,
        "state_changed",
        ticket_id=ticket_id,
        payload={"from": src, "to": dst, "actor": actor},
    )
    conn.commit()
    obs.emit("state_changed", ticket_id=ticket_id, src=src, dst=dst, actor=actor)
    feed.publish("cycle", f"{src} → {dst}", ticket_id=ticket_id, state=dst)
    return get_ticket(conn, ticket_id)


def start_run(conn: sqlite3.Connection, ticket_id: str, stage: str) -> dict:
    rid = _id("run")
    conn.execute(
        """INSERT INTO runs (id, ticket_id, stage, status, started_at)
           VALUES (?, ?, ?, 'running', ?)""",
        (rid, ticket_id, stage, _now()),
    )
    add_event(conn, "run_started", ticket_id=ticket_id, run_id=rid, payload={"stage": stage})
    conn.commit()
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone())


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    ok: bool,
    stdout: str,
    stderr: str,
    session_id: str | None = None,
) -> None:
    conn.execute(
        """UPDATE runs SET status = ?, stdout = ?, stderr = ?, session_id = ?, finished_at = ?
           WHERE id = ?""",
        ("ok" if ok else "failed", stdout, stderr, session_id, _now(), run_id),
    )
    add_event(
        conn,
        "run_finished",
        run_id=run_id,
        payload={"ok": ok, "stderr": stderr[:500]},
    )
    conn.commit()


def claim_auto(conn: sqlite3.Connection) -> dict | None:
    """One unlocked ticket whose next step is an auto runner edge."""
    for src, dst in (
        ("ready_to_plan", "planning"),
        ("implementing", None),  # already in stage — runner owns the work
        ("validating", None),
        ("pr_open", "merge_review"),
        ("integrating", None),
        ("planning", None),
    ):
        row = conn.execute(
            "SELECT * FROM tickets WHERE state = ? ORDER BY updated_at ASC LIMIT 1",
            (src,),
        ).fetchone()
        if row:
            return dict(row)
    return None
