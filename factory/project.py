"""Projects: slug, workflow JSON, connections. Small surface."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from factory.db import row_dict

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

DEFAULT_WORKFLOW = [
    {"id": "inbox", "title": "Inbox", "kind": "human"},
    {"id": "planning", "title": "Plan", "kind": "agent", "file": "agents/plan.md"},
    {"id": "plan_review", "title": "Review", "kind": "human"},
    {"id": "implementing", "title": "Build", "kind": "agent", "file": "agents/build.md"},
    {"id": "validating", "title": "Check", "kind": "plugin", "plugin": "validate"},
    {"id": "merge_review", "title": "Merge", "kind": "human"},
    {"id": "integrating", "title": "Ship", "kind": "plugin", "plugin": "integrate"},
]


def infer_stage_status(state: str) -> tuple[str, str]:
    """Map a legacy SM state onto (stage, status)."""
    return {
        "inbox": ("inbox", "ready"),
        "proposed": ("inbox", "ready"),
        "ready_to_plan": ("planning", "ready"),
        "planning": ("planning", "running"),
        "plan_review": ("plan_review", "ready"),
        "implementing": ("implementing", "running"),
        "ready_to_validate": ("validating", "ready"),
        "validating": ("validating", "running"),
        "pr_open": ("merge_review", "ready"),
        "merge_review": ("merge_review", "ready"),
        "integrating": ("integrating", "running"),
        "done": ("integrating", "done"),
        "failed": ("implementing", "blocked"),
        "needs_human": ("plan_review", "blocked"),
    }.get(state or "", ("inbox", "ready"))


def legacy_state(stage: str | None, status: str | None, fallback: str = "inbox") -> str:
    """Keep the old `state` column in sync so the current board still groups."""
    if status == "done":
        return "done"
    if status == "blocked":
        return "failed"
    if not stage:
        return fallback
    if stage == "planning":
        return "planning" if status == "running" else "ready_to_plan"
    if stage == "validating":
        return "validating" if status == "running" else "ready_to_validate"
    if stage == "integrating":
        return "integrating"
    return stage


def workspace_root() -> Path:
    raw = os.environ.get("FACTORY_ROOT")
    if raw:
        return Path(raw)
    # parent of this checkout (…/projects/factory → …/projects)
    return Path(__file__).resolve().parents[2]


def parse_workflow(raw: str | None) -> list[dict]:
    if not raw:
        return list(DEFAULT_WORKFLOW)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return list(DEFAULT_WORKFLOW)
    if isinstance(data, list) and data:
        return data
    return list(DEFAULT_WORKFLOW)


def workflow_of(proj: dict) -> list[dict]:
    return parse_workflow(proj.get("workflow"))


def occupied_stage_ids(conn, project_id: str) -> set[str]:
    rows = conn.execute(
        """SELECT DISTINCT stage FROM tickets
           WHERE project_id = ? AND status != 'done' AND stage IS NOT NULL""",
        (project_id,),
    ).fetchall()
    return {r["stage"] for r in rows if r["stage"]}


def validate_workflow_patch(old: list[dict], new: list[dict], occupied: set[str]) -> None:
    from factory.machine import IllegalTransition

    old_ids = {s["id"] for s in old}
    new_by = {s["id"]: s for s in new}
    for sid in occupied:
        if sid not in new_by:
            raise IllegalTransition(f"cannot delete occupied stage: {sid}")
        prev = next((s for s in old if s["id"] == sid), None)
        if prev and prev.get("kind") != new_by[sid].get("kind"):
            raise IllegalTransition(f"cannot change kind of occupied stage: {sid}")
    for s in new:
        if s.get("id") not in old_ids and s.get("kind") not in {"human", "agent", "plugin"}:
            raise IllegalTransition(f"bad kind: {s.get('kind')}")


def get(conn, slug: str) -> dict | None:
    row = row_dict(conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone())
    if not row:
        return None
    row["workflow"] = workflow_of(row)
    row["name"] = row.get("name") or row["slug"]
    return decorate(conn, row)


def list_projects(conn) -> list[dict]:
    out = []
    for row in conn.execute("SELECT * FROM projects ORDER BY created_at"):
        d = dict(row)
        d["workflow"] = workflow_of(d)
        d["name"] = d.get("name") or d["slug"]
        out.append(decorate(conn, d))
    return out


def decorate(conn, proj: dict) -> dict:
    """Counts + connection flags. Never includes secret values."""
    pid = proj["id"]
    open_n = conn.execute(
        "SELECT COUNT(*) n FROM tickets WHERE project_id = ? AND state != 'done'",
        (pid,),
    ).fetchone()["n"]
    file_n = conn.execute(
        "SELECT COUNT(*) n FROM files WHERE project_id = ? AND store = 'repo'",
        (pid,),
    ).fetchone()["n"]
    proj["open_tickets"] = open_n
    proj["file_count"] = file_n
    proj["connections"] = connections_of(conn, proj)
    return proj


def connections_of(conn, proj: dict) -> dict:
    from factory import apps as apps_mod

    listed = apps_mod.list_apps(conn, proj["id"], git_remote=proj.get("git_remote"))
    gh = next((a for a in listed if a["id"] == "github"), None)
    cf_app = next((a for a in listed if a["id"] == "cloudflare"), None)
    repo = (gh or {}).get("resource", {}).get("repo") or None
    cf_res = (cf_app or {}).get("resource") or {}
    cf = {
        "set": bool((cf_app or {}).get("installed")),
        "token_set": bool((cf_app or {}).get("installed")),
        "account_id": cf_res.get("account_id") or "",
        "pages_project": cf_res.get("pages_project") or "",
        "r2_bucket": cf_res.get("r2_bucket") or "",
    }
    return {
        "git": proj.get("git_remote") or (f"https://github.com/{repo}.git" if repo else None),
        "cloudflare": cf,
        "apps": listed,
    }


SEED_FILES = {
    "pipeline.yml": (
        "# Executed as a shell script. ${vault/NAME} resolves from env.\n"
        "# Connect Cloudflare in settings; dispatch never invents steps.\n"
        "true\n"
    ),
    "instances/dev.json": json.dumps({"name": "dev", "production": False}, indent=2) + "\n",
    "instances/prod.json": json.dumps({"name": "prod", "production": True}, indent=2) + "\n",
    "handlers/default.yml": "match: {}\nautonomy: false\nissue:\n  title: inbound\n",
    "context/readme.md": "# context\n\nConstitution, taste, and design live here.\n",
    "agents/plan.md": "Write a short plan for the ticket. Do not implement.\n",
    "agents/build.md": "Implement the ticket. Stay inside this repository.\n",
}


def seed_files(conn, project_id: str) -> None:
    from factory import files as files_mod

    existing = {f["path"] for f in files_mod.list_prefix(conn, project_id, "", "repo")}
    for path, body in SEED_FILES.items():
        if path not in existing:
            files_mod.put(conn, project_id, path, body)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:63]


def create(
    conn,
    *,
    slug: str,
    name: str | None = None,
    validate_cmd: str = "true",
    git_remote: str | None = None,
    owner_id: str | None = None,
    mode: str = "live",
    quality_gates: bool = False,
    template_id: str | None = None,
) -> dict:
    if mode not in ("live", "tickets"):
        raise ValueError(f"bad mode: {mode!r}")
    slug = (slug or slugify(name or "")).strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    if conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        raise ValueError(f"project exists: {slug}")
    dest = workspace_root() / slug
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
        (dest / "README.md").write_text(f"# {name or slug}\n")
        subprocess.run(["git", "add", "-A"], cwd=dest, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=factory@local", "-c", "user.name=factory", "commit", "-m", "init"],
            cwd=dest,
            capture_output=True,
        )
    pid = f"prj_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO projects
           (id, slug, name, repo_path, validate_cmd, infra_plugin, created_at, workflow,
            git_remote, owner_id, mode, quality_gates, template_id)
           VALUES (?, ?, ?, ?, ?, 'none', ?, ?, ?, ?, ?, ?, ?)""",
        (
            pid,
            slug,
            (name or slug).strip(),
            str(dest),
            validate_cmd,
            now,
            json.dumps(DEFAULT_WORKFLOW),
            git_remote,
            owner_id,
            mode,
            1 if quality_gates else 0,
            template_id,
        ),
    )
    conn.commit()
    return get(conn, slug)


def patch(
    conn,
    slug: str,
    *,
    name: str | None = None,
    validate_cmd: str | None = None,
    mode: str | None = None,
    quality_gates: bool | None = None,
) -> dict:
    proj = get(conn, slug)
    if not proj:
        raise KeyError(slug)
    if name is not None:
        conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name.strip() or proj["slug"], proj["id"]))
    if validate_cmd is not None:
        conn.execute("UPDATE projects SET validate_cmd = ? WHERE id = ?", (validate_cmd, proj["id"]))
    if mode is not None:
        if mode not in ("live", "tickets"):
            raise ValueError(f"bad mode: {mode!r}")
        conn.execute("UPDATE projects SET mode = ? WHERE id = ?", (mode, proj["id"]))
    if quality_gates is not None:
        conn.execute(
            "UPDATE projects SET quality_gates = ? WHERE id = ?",
            (1 if quality_gates else 0, proj["id"]),
        )
    conn.commit()
    return get(conn, slug)


def delete(conn, slug: str) -> None:
    proj = get(conn, slug)
    if not proj:
        raise KeyError(slug)
    pid = proj["id"]
    conn.execute("DELETE FROM tickets WHERE project_id = ?", (pid,))
    conn.execute("DELETE FROM files WHERE project_id = ?", (pid,))
    conn.execute("DELETE FROM messages WHERE project_id = ?", (pid,))
    conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    conn.commit()


def connect_git(conn, slug: str, remote: str) -> dict:
    """Record a GitHub remote and clone/fetch into FACTORY_ROOT/<slug>."""
    proj = get(conn, slug)
    if not proj:
        raise KeyError(slug)
    remote = (remote or "").strip()
    if not (
        remote.startswith("https://")
        or remote.startswith("git@")
        or remote.startswith("file://")
    ):
        raise ValueError("git remote must be https://, git@, or file://")
    dest = Path(proj["repo_path"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN", "")
    clone_url = remote
    if token and remote.startswith("https://github.com/"):
        clone_url = remote.replace("https://github.com/", f"https://x-access-token:{token}@github.com/", 1)
    if (dest / ".git").exists():
        subprocess.run(["git", "remote", "remove", "origin"], cwd=dest, capture_output=True)
        r = subprocess.run(
            ["git", "remote", "add", "origin", clone_url],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "fetch", "origin"], cwd=dest, capture_output=True)
    else:
        if dest.exists() and any(dest.iterdir()):
            # leave non-empty non-git dirs alone; clone beside would confuse repo_path
            raise ValueError(f"repo_path exists and is not a git checkout: {dest}")
        if dest.exists():
            dest.rmdir()
        r = subprocess.run(
            ["git", "clone", clone_url, str(dest)],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise ValueError((r.stderr or r.stdout or "git clone failed")[:400])
    # scrub token from stored remote
    conn.execute("UPDATE projects SET git_remote = ? WHERE id = ?", (remote, proj["id"]))
    conn.commit()
    from factory import apps as apps_mod

    repo = apps_mod._repo_from_remote(remote)
    prev = apps_mod._read_meta(conn, proj["id"], "apps/github.json")
    apps_mod._write_meta(
        conn,
        proj["id"],
        "apps/github.json",
        {
            **prev,
            "installed": True,
            "repo": repo,
            "identity": prev.get("identity") or "",
        },
    )
    return get(conn, slug)


def connect_cloudflare(
    conn,
    slug: str,
    *,
    account_id: str = "",
    api_token: str = "",
    pages_project: str = "",
    r2_bucket: str = "",
    r2_access_key_id: str = "",
    r2_secret_access_key: str = "",
) -> dict:
    """Persist CF connection metadata as a file; tokens go to the vault."""
    from factory import files as files_mod

    proj = get(conn, slug)
    if not proj:
        raise KeyError(slug)
    pid = proj["id"]
    if api_token.strip():
        files_mod.put(conn, pid, "CF_API_TOKEN", api_token.strip(), store="vault")
    if r2_access_key_id.strip():
        files_mod.put(conn, pid, "R2_ACCESS_KEY_ID", r2_access_key_id.strip(), store="vault")
    if r2_secret_access_key.strip():
        files_mod.put(conn, pid, "R2_SECRET_ACCESS_KEY", r2_secret_access_key.strip(), store="vault")
    existing: dict = {}
    try:
        raw = files_mod.get(conn, pid, "instances/cloudflare.json")
        existing = json.loads(raw.get("body") or "{}")
        if not isinstance(existing, dict):
            existing = {}
    except (files_mod.FileError, json.JSONDecodeError, TypeError):
        existing = {}
    meta = {
        "account_id": (account_id or existing.get("account_id") or "").strip(),
        "pages_project": (pages_project or existing.get("pages_project") or "").strip(),
        "r2_bucket": (r2_bucket or existing.get("r2_bucket") or "").strip(),
    }
    files_mod.put(conn, pid, "instances/cloudflare.json", json.dumps(meta, indent=2) + "\n")
    from factory import apps as apps_mod

    prev = apps_mod._read_meta(conn, pid, "apps/cloudflare.json")
    apps_mod._write_meta(
        conn,
        pid,
        "apps/cloudflare.json",
        {
            **prev,
            "installed": True,
            "account_id": meta["account_id"],
            "account_name": prev.get("account_name") or meta["account_id"],
            "pages_project": meta["pages_project"],
            "r2_bucket": meta["r2_bucket"],
            "identity": prev.get("identity") or meta["account_id"],
        },
    )
    return get(conn, slug)


def save_workflow(conn, project_id: str, workflow: list[dict]) -> None:
    conn.execute(
        "UPDATE projects SET workflow = ? WHERE id = ?",
        (json.dumps(workflow), project_id),
    )
    conn.commit()
