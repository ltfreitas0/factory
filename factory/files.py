"""Project files: one API, two roots (repo | vault).

Vault values are never returned after write. Paths are relative; `..` is
rejected. Isolated from tickets and the board.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone

SAFE = re.compile(r"^[A-Za-z0-9._/-]+$")


class FileError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(path: str) -> str:
    p = (path or "").strip().lstrip("/")
    if not p or p in {".", ".."}:
        raise FileError("invalid path")
    parts = p.split("/")
    if any(part in {"", ".", ".."} for part in parts) or not SAFE.match(p):
        raise FileError(f"illegal path: {path!r}")
    return p


def ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS files (
             project_id TEXT NOT NULL,
             store TEXT NOT NULL,
             path TEXT NOT NULL,
             body TEXT,
             updated_at TEXT NOT NULL,
             PRIMARY KEY (project_id, store, path)
           )"""
    )
    conn.commit()


def put(conn, project_id: str, path: str, body: str, store: str = "repo") -> dict:
    if store not in {"repo", "vault"}:
        raise FileError(f"bad store: {store}")
    path = normalize(path)
    if store == "vault":
        # store a hash for compare; raw only lives in this write if caller
        # keeps it. We persist HMAC-sha256 of the value with a static local
        # pepper so GET never has material to leak.
        body = _digest(body)
    ensure_schema(conn)
    conn.execute(
        """INSERT INTO files (project_id, store, path, body, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(project_id, store, path) DO UPDATE SET
             body = excluded.body, updated_at = excluded.updated_at""",
        (project_id, store, path, body, _now()),
    )
    conn.commit()
    return describe(store, path, set_=True)


def get(conn, project_id: str, path: str, store: str = "repo") -> dict:
    path = normalize(path)
    ensure_schema(conn)
    row = conn.execute(
        "SELECT body, updated_at FROM files WHERE project_id=? AND store=? AND path=?",
        (project_id, store, path),
    ).fetchone()
    if row is None:
        raise FileError(f"not found: {store}:{path}")
    if store == "vault":
        return describe(store, path, set_=True, updated_at=row["updated_at"])
    return {
        "store": store,
        "path": path,
        "body": row["body"],
        "set": True,
        "updated_at": row["updated_at"],
    }


def delete(conn, project_id: str, path: str, store: str = "repo") -> None:
    path = normalize(path)
    ensure_schema(conn)
    conn.execute(
        "DELETE FROM files WHERE project_id=? AND store=? AND path=?",
        (project_id, store, path),
    )
    conn.commit()


def list_prefix(conn, project_id: str, prefix: str = "", store: str = "repo") -> list[dict]:
    prefix = prefix.strip().lstrip("/")
    if prefix:
        normalize(prefix.rstrip("/"))
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT path, updated_at FROM files
           WHERE project_id=? AND store=? AND path LIKE ?
           ORDER BY path""",
        (project_id, store, f"{prefix}%" if prefix else "%"),
    ).fetchall()
    return [describe(store, r["path"], set_=True, updated_at=r["updated_at"]) for r in rows]


def vault_matches(conn, project_id: str, path: str, plaintext: str) -> bool:
    """Constant-time compare of a presented secret to the stored digest."""
    path = normalize(path)
    ensure_schema(conn)
    row = conn.execute(
        "SELECT body FROM files WHERE project_id=? AND store='vault' AND path=?",
        (project_id, path),
    ).fetchone()
    if row is None or not row["body"]:
        return False
    return hmac.compare_digest(row["body"], _digest(plaintext))


def describe(store: str, path: str, set_: bool, updated_at: str | None = None) -> dict:
    return {"store": store, "path": path, "set": set_, "updated_at": updated_at}


def _digest(value: str) -> str:
    # Local-only pepper; sandbox. Real encryption can replace this later
    # without changing the files surface.
    return hmac.new(b"factory-vault-v1", (value or "").encode(), hashlib.sha256).hexdigest()
