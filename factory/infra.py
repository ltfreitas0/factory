"""Infra records: branches, snapshots, instances, deployments.

Pure persistence + rules around the ratified data model (DATA.md). No
Cloudflare calls here — snapshot object copies go through `files`, build/run
goes through `sandbox`, deploy goes through `dispatch`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from factory import files
from factory.db import row_dict, rows

DEFAULT_INSTANCES = [
    {"name": "dev", "production": False, "kind": "sandbox", "account": "platform"},
    {"name": "prod", "production": True, "kind": "pages", "account": "user"},
]


class InfraError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# --- branches -------------------------------------------------------------

def ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS branches (
             id TEXT PRIMARY KEY,
             project_id TEXT NOT NULL,
             name TEXT NOT NULL,
             kind TEXT NOT NULL DEFAULT 'feature',
             head_sha TEXT,
             created_at TEXT NOT NULL,
             UNIQUE (project_id, name)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
             id TEXT PRIMARY KEY,
             project_id TEXT NOT NULL,
             branch_id TEXT,
             sha TEXT NOT NULL,
             message TEXT,
             created_by TEXT NOT NULL DEFAULT 'user',
             created_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS instances (
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
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deployments (
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
           )"""
    )
    conn.commit()


def list_branches(conn, project_id: str) -> list[dict]:
    return rows(
        conn.execute(
            "SELECT * FROM branches WHERE project_id = ? ORDER BY created_at", (project_id,)
        )
    )


def get_branch(conn, project_id: str, name: str) -> dict | None:
    return row_dict(
        conn.execute(
            "SELECT * FROM branches WHERE project_id = ? AND name = ?", (project_id, name)
        ).fetchone()
    )


def create_branch(conn, project_id: str, name: str, kind: str = "feature", from_sha: str | None = None) -> dict:
    name = (name or "").strip().lower()
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise InfraError(f"bad branch name: {name!r}")
    if kind not in ("main", "dev", "prod", "feature"):
        raise InfraError(f"bad branch kind: {kind!r}")
    if get_branch(conn, project_id, name):
        raise InfraError(f"branch exists: {name}")
    bid = _id("brn")
    head = from_sha or ""
    conn.execute(
        """INSERT INTO branches (id, project_id, name, kind, head_sha, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (bid, project_id, name, kind, head, _now()),
    )
    conn.commit()
    return get_branch(conn, project_id, name)


def ensure_default_branches(conn, project_id: str) -> None:
    for name, kind in (("main", "main"), ("dev", "dev"), ("prod", "prod")):
        if not get_branch(conn, project_id, name):
            create_branch(conn, project_id, name, kind)


# --- snapshots ------------------------------------------------------------

def list_snapshots(conn, project_id: str) -> list[dict]:
    return rows(
        conn.execute(
            "SELECT * FROM snapshots WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
        )
    )


def create_snapshot(
    conn, project_id: str, *, sha: str | None = None, message: str = "",
    branch_id: str | None = None, created_by: str = "user",
) -> dict:
    """Copy current repo state to snapshots/{sha}/ and record it.

    sha defaults to a short random id; callers that want git-derived shas
    pass their own.
    """
    sha = sha or _id("snap")
    n = files.snapshot_state(conn, project_id, sha)
    if n < 0:
        raise InfraError("snapshot failed (no storage backend)")
    sid = _id("snp")
    conn.execute(
        """INSERT INTO snapshots (id, project_id, branch_id, sha, message, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, project_id, branch_id, sha, message, created_by, _now()),
    )
    conn.commit()
    return row_dict(conn.execute("SELECT * FROM snapshots WHERE id = ?", (sid,)).fetchone())


# --- instances ------------------------------------------------------------

def list_instances(conn, project_id: str) -> list[dict]:
    return rows(
        conn.execute(
            "SELECT * FROM instances WHERE project_id = ? ORDER BY created_at", (project_id,)
        )
    )


def get_instance(conn, project_id: str, name: str) -> dict | None:
    return row_dict(
        conn.execute(
            "SELECT * FROM instances WHERE project_id = ? AND name = ?", (project_id, name)
        ).fetchone()
    )


def create_instance(
    conn, project_id: str, *, name: str, production: bool = False,
    kind: str = "sandbox", account: str = "platform", url: str = "", branch: str = "",
) -> dict:
    if name not in ("dev", "prod", "canary", "experimental") and not name.strip():
        raise InfraError(f"bad instance name: {name!r}")
    if kind not in ("sandbox", "workers", "pages"):
        raise InfraError(f"bad instance kind: {kind!r}")
    if account not in ("platform", "user"):
        raise InfraError(f"bad instance account: {account!r}")
    if production:
        existing = conn.execute(
            "SELECT 1 FROM instances WHERE project_id = ? AND production = 1", (project_id,)
        ).fetchone()
        if existing:
            raise InfraError("production instance already exists")
    if get_instance(conn, project_id, name):
        raise InfraError(f"instance exists: {name}")
    iid = _id("ins")
    conn.execute(
        """INSERT INTO instances (id, project_id, name, production, kind, account, url, branch, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (iid, project_id, name, 1 if production else 0, kind, account, url, branch, _now()),
    )
    conn.commit()
    return get_instance(conn, project_id, name)


def ensure_default_instances(conn, project_id: str) -> None:
    if conn.execute("SELECT 1 FROM instances WHERE project_id = ?", (project_id,)).fetchone():
        return
    for spec in DEFAULT_INSTANCES:
        create_instance(conn, project_id, **spec)


# --- deployments ----------------------------------------------------------

DEPLOY_STATUSES = ("provisioning", "ready", "failed", "destroying", "destroyed")


def list_deployments(conn, project_id: str) -> list[dict]:
    return rows(
        conn.execute(
            """SELECT d.*, i.name AS instance_name, i.production AS production
               FROM deployments d JOIN instances i ON i.id = d.instance_id
               WHERE d.project_id = ? ORDER BY d.created_at DESC""",
            (project_id,),
        )
    )


def get_deployment(conn, deployment_id: str) -> dict | None:
    return row_dict(
        conn.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,)).fetchone()
    )


def create_deployment(
    conn, project_id: str, *, instance_id: str, branch: str = "", sha: str = "",
    deployed_by: str = "user",
) -> dict:
    inst = row_dict(
        conn.execute("SELECT * FROM instances WHERE id = ? AND project_id = ?", (instance_id, project_id)).fetchone()
    )
    if not inst:
        raise InfraError("unknown instance")
    did = _id("dep")
    conn.execute(
        """INSERT INTO deployments
           (id, project_id, instance_id, branch, sha, status, deployed_by, created_at)
           VALUES (?, ?, ?, ?, ?, 'provisioning', ?, ?)""",
        (did, project_id, instance_id, branch, sha, deployed_by, _now()),
    )
    conn.commit()
    return get_deployment(conn, did)


def set_deployment_status(conn, deployment_id: str, status: str, *, url: str = "", infra_ref: str = "") -> dict:
    if status not in DEPLOY_STATUSES:
        raise InfraError(f"bad status: {status!r}")
    sets = ["status = ?", "finished_at = ?"]
    params: list = [status, _now() if status in ("ready", "failed", "destroyed") else None]
    if url:
        sets.append("url = ?")
        params.append(url)
    if infra_ref:
        sets.append("infra_ref = ?")
        params.append(infra_ref)
    params.append(deployment_id)
    conn.execute(f"UPDATE deployments SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    return get_deployment(conn, deployment_id)
