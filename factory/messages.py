"""Inbound tape: untyped messages, one ingest token per project.

Handler policy lives in repo files under handlers/. This module does not
know HTTP. Tickets are created by the caller from a Decision.
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from factory import files
from factory.machine import apply as sm_apply


INGEST_PATH = "INGEST_TOKEN"


class AuthError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"


def ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
             id TEXT PRIMARY KEY,
             project_id TEXT NOT NULL,
             source TEXT NOT NULL,
             payload TEXT NOT NULL,
             at TEXT NOT NULL,
             handled_at TEXT,
             result TEXT
           )"""
    )
    conn.commit()
    files.ensure_schema(conn)


def rotate_ingest_token(conn, project_id: str) -> str:
    """Replace the project's only ingest token. Returns plaintext once."""
    token = secrets.token_urlsafe(32)
    files.put(conn, project_id, INGEST_PATH, token, store="vault")
    return token


def check_ingest_token(conn, project_id: str, token: str | None) -> None:
    if not token or not files.vault_matches(conn, project_id, INGEST_PATH, token):
        raise AuthError("invalid ingest token")


def ingest(
    conn, project_id: str, token: str | None, source: str, payload
) -> dict:
    ensure_schema(conn)
    check_ingest_token(conn, project_id, token)
    return add(conn, project_id, source, payload)


def add(conn, project_id: str, source: str, payload) -> dict:
    """Insert a message without token auth (authenticated chat path)."""
    ensure_schema(conn)
    mid = _id()
    body = json.dumps(payload)
    conn.execute(
        """INSERT INTO messages (id, project_id, source, payload, at)
           VALUES (?, ?, ?, ?, ?)""",
        (mid, project_id, source or "unknown", body, _now()),
    )
    conn.commit()
    return get(conn, mid)


def get(conn, message_id: str) -> dict:
    row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if row is None:
        raise KeyError(message_id)
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except json.JSONDecodeError:
            pass
    return d


def list_messages(conn, project_id: str) -> list[dict]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM messages WHERE project_id=? ORDER BY at", (project_id,)
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


@dataclass
class Decision:
    drop: bool
    autonomy: bool = False
    target: str | None = None
    handler: str | None = None


def load_handlers(conn, project_id: str) -> list[dict]:
    """Parse handlers/*.yml as tiny {match, autonomy, issue.stage} dicts.

    YAML is optional; JSON objects in the file body are also accepted.
    A file that is just `autonomy: false` is a catch-all.
    """
    listed = files.list_prefix(conn, project_id, "handlers/", store="repo")
    out = []
    for meta in listed:
        raw = files.get(conn, project_id, meta["path"])["body"] or ""
        spec = _parse_handler(raw)
        spec["path"] = meta["path"]
        out.append(spec)
    return out


def _parse_handler(raw: str) -> dict:
    spec = {"autonomy": False, "source": None, "target": None}
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("autonomy:"):
            spec["autonomy"] = line.split(":", 1)[1].strip().lower() in {"true", "yes", "1"}
        elif line.startswith("source:"):
            spec["source"] = line.split(":", 1)[1].strip().strip("'\"")
        elif line.startswith("stage:") or line.endswith("stage:") or "issue.stage" in line:
            spec["target"] = line.split(":", 1)[1].strip().strip("'\"")
        elif line.startswith("match:"):
            spec["match"] = line.split(":", 1)[1].strip()
    return spec


def decide(handlers: list[dict], source: str, payload: dict) -> Decision:
    if not handlers:
        return Decision(drop=True, handler=None)
    for h in handlers:
        if h.get("source") and h["source"] != source:
            continue
        return Decision(
            drop=False,
            autonomy=bool(h.get("autonomy")),
            target=h.get("target"),
            handler=h.get("path"),
        )
    return Decision(drop=True)


def process(conn, project_id: str, message_id: str, workflow: list[dict]) -> dict:
    """Ack a message. If a handler matches, land a ticket dict (not persisted)."""
    ensure_schema(conn)
    msg = get(conn, message_id)
    if msg.get("handled_at"):
        return msg
    decision = decide(load_handlers(conn, project_id), msg["source"], msg["payload"])
    result = {"drop": decision.drop, "handler": decision.handler, "autonomy": decision.autonomy}
    ticket = None
    if not decision.drop:
        ticket = sm_apply(
            workflow,
            {},
            {
                "name": "land",
                "actor": "handler",
                "autonomy": decision.autonomy,
                "target": decision.target,
            },
        )
        result["ticket"] = ticket
    conn.execute(
        "UPDATE messages SET handled_at=?, result=? WHERE id=?",
        (_now(), json.dumps(result), message_id),
    )
    conn.commit()
    return get(conn, message_id)
