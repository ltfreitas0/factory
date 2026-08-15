"""Projects: slug, workflow JSON, connections. Small surface."""

from __future__ import annotations

import json
import os
from pathlib import Path

from factory.db import row_dict

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
    return Path("/home/bix/projects")


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
    return row


def save_workflow(conn, project_id: str, workflow: list[dict]) -> None:
    conn.execute(
        "UPDATE projects SET workflow = ? WHERE id = ?",
        (json.dumps(workflow), project_id),
    )
    conn.commit()
