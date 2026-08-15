"""Ticket/document/run writes. The control plane's only mutation path."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from pathlib import Path

from factory import feed, obs, sm
from factory.db import row_dict, rows
from factory.machine import IllegalTransition as MachineIllegal
from factory.machine import apply as machine_apply
from factory.project import infer_stage_status, legacy_state, workflow_of


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
        """INSERT INTO projects (id, slug, repo_path, validate_cmd, infra_plugin, created_at, workflow)
           VALUES (?, ?, ?, ?, 'none', ?, ?)""",
        (pid, slug, repo_path, validate_cmd, _now(), json.dumps(__import__("factory.project", fromlist=["DEFAULT_WORKFLOW"]).DEFAULT_WORKFLOW)),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone())


def ensure_playground(conn: sqlite3.Connection, repo_path: str) -> dict:
    return ensure_project(conn, "playground", repo_path, "python3 -m pytest -q")


def active_ticket(conn: sqlite3.Connection) -> dict | None:
    return row_dict(
        conn.execute(
            """SELECT * FROM tickets
               WHERE state NOT IN ('done', 'failed', 'needs_human', 'inbox', 'proposed')
               ORDER BY updated_at DESC LIMIT 1"""
        ).fetchone()
    )


def create_ticket(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    title: str,
    body: str,
    kind: str = "build",
    source: str = "human",
    parent_id: str | None = None,
) -> dict:
    tid = _id("tkt")
    now = _now()
    if kind not in ("build", "validate"):
        kind = "build"
    if source not in ("human", "factory"):
        source = "human"
    state = "proposed" if source == "factory" else "inbox"
    stage, status = infer_stage_status(state)
    conn.execute(
        """INSERT INTO tickets
           (id, project_id, state, title, body, kind, source, parent_id,
            created_at, updated_at, stage, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tid, project_id, state, title, body, kind, source, parent_id, now, now, stage, status),
    )
    add_event(
        conn,
        "ticket_created",
        ticket_id=tid,
        payload={"title": title, "kind": kind, "source": source, "state": state},
    )
    conn.commit()
    return get_ticket(conn, tid)


def spawn_from_repo(
    conn: sqlite3.Connection, *, project_id: str, repo: Path, parent_id: str | None = None
) -> list[dict]:
    """Create proposed tickets from repo .meta/spawn.json. Never auto-starts them.

    File shape: {"tickets": [{"title", "body", "kind"?}, ...]}.
    Skip a title that already exists on this project.
    """
    path = repo / ".meta" / "spawn.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    items = payload.get("tickets") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    created: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        kind = str(item.get("kind") or "build")
        if not title:
            continue
        exists = conn.execute(
            "SELECT 1 FROM tickets WHERE project_id = ? AND title = ? LIMIT 1",
            (project_id, title),
        ).fetchone()
        if exists:
            continue
        created.append(
            create_ticket(
                conn,
                project_id=project_id,
                title=title,
                body=body,
                kind=kind,
                source="factory",
                parent_id=parent_id,
            )
        )
    return created


def append_feed(conn: sqlite3.Connection, item: dict) -> None:
    conn.execute(
        """INSERT INTO feed_log (at, kind, text, ticket_id, state, title)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            item.get("at") or "",
            item.get("kind") or "",
            item.get("text") or "",
            item.get("ticket_id"),
            item.get("state"),
            item.get("title"),
        ),
    )
    conn.commit()


def load_feed(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    rows_ = rows(
        conn.execute(
            "SELECT at, kind, text, ticket_id, state, title FROM feed_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )
    rows_.reverse()
    if rows_:
        return rows_
    # first boot after restart: reconstruct cycle from events and persist
    evs = rows(
        conn.execute(
            """SELECT at, payload, ticket_id FROM events
               WHERE type = 'state_changed' ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
    )
    evs.reverse()
    out = []
    for e in evs:
        try:
            p = json.loads(e["payload"] or "{}")
        except json.JSONDecodeError:
            p = {}
        src, dst = p.get("from"), p.get("to")
        if not dst:
            continue
        at = (e["at"] or "")[11:19]
        item = {
            "at": at,
            "kind": "cycle",
            "text": f"{src} → {dst}" if src else str(dst),
            "ticket_id": e["ticket_id"],
            "state": dst,
            "title": None,
        }
        append_feed(conn, item)
        out.append(item)
    return out


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


def list_tickets(conn: sqlite3.Connection, project: str | None = None) -> list[dict]:
    sql = """SELECT t.*, p.slug AS project FROM tickets t
             JOIN projects p ON p.id = t.project_id"""
    if project:
        out = rows(
            conn.execute(sql + " WHERE p.slug = ? ORDER BY t.created_at DESC", (project,))
        )
    else:
        out = rows(conn.execute(sql + " ORDER BY t.created_at DESC"))
    from factory import cost

    rolls = cost.ticket_rollups(conn)
    for t in out:
        part = rolls.get(t["id"]) or {"tokens": 0, "usd": 0.0}
        t["tokens"] = part.get("tokens") or 0
        t["usd"] = part.get("usd") or 0.0
    return out


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
    stage, status = infer_stage_status(dst)
    conn.execute(
        "UPDATE tickets SET state = ?, stage = ?, status = ?, updated_at = ? WHERE id = ?",
        (dst, stage, status, now, ticket_id),
    )
    add_event(
        conn,
        "state_changed",
        ticket_id=ticket_id,
        payload={"from": src, "to": dst, "actor": actor},
    )
    conn.commit()
    obs.emit("state_changed", ticket_id=ticket_id, src=src, dst=dst, actor=actor)
    feed.publish(
        "cycle",
        f"{src} → {dst}",
        ticket_id=ticket_id,
        state=dst,
        title=t.get("title"),
    )
    return get_ticket(conn, ticket_id)


def apply_action(
    conn: sqlite3.Connection,
    ticket_id: str,
    action: dict,
) -> dict:
    """Apply a generic machine action; keep legacy `state` in sync."""
    t = get_ticket(conn, ticket_id)
    if not t:
        raise KeyError(ticket_id)
    proj = conn.execute("SELECT * FROM projects WHERE id = ?", (t["project_id"],)).fetchone()
    wf = workflow_of(dict(proj) if proj else {})
    cur = {"stage": t.get("stage") or infer_stage_status(t["state"])[0], "status": t.get("status") or infer_stage_status(t["state"])[1]}
    nxt = machine_apply(wf, cur, action)
    state = legacy_state(nxt.get("stage"), nxt.get("status"), t["state"])
    conn.execute(
        """UPDATE tickets SET stage = ?, status = ?, state = ?, updated_at = ?
           WHERE id = ?""",
        (nxt.get("stage"), nxt.get("status"), state, _now(), ticket_id),
    )
    add_event(
        conn,
        "state_changed",
        ticket_id=ticket_id,
        payload={
            "from": t.get("stage"),
            "to": nxt.get("stage"),
            "status": nxt.get("status"),
            "action": action.get("name"),
            "actor": action.get("actor"),
        },
    )
    conn.commit()
    obs.emit(
        "state_changed",
        ticket_id=ticket_id,
        src=t.get("stage"),
        dst=nxt.get("stage"),
        actor=action.get("actor"),
        action=action.get("name"),
    )
    feed.publish(
        "cycle",
        f"{t.get('stage')} → {nxt.get('stage')}",
        ticket_id=ticket_id,
        state=state,
        title=t.get("title"),
    )
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
    row = conn.execute(
        """SELECT t.* FROM tickets t
           JOIN projects p ON p.id = t.project_id
           WHERE t.status = 'ready' AND t.stage IS NOT NULL
           ORDER BY t.updated_at ASC"""
    ).fetchall()
    for r in row:
        proj = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (r["project_id"],)).fetchone())
        wf = workflow_of(proj)
        st = next((s for s in wf if s.get("id") == r["stage"]), None)
        if st and st.get("kind") in {"agent", "plugin"} and not st.get("muted"):
            return dict(r)
    for src in (
        "ready_to_plan",
        "ready_to_validate",
        "implementing",
        "validating",
        "pr_open",
        "integrating",
        "planning",
    ):
        old = conn.execute(
            "SELECT * FROM tickets WHERE state = ? ORDER BY updated_at ASC LIMIT 1",
            (src,),
        ).fetchone()
        if old:
            return dict(old)
    return None
