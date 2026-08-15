"""Local dispatcher. Dumb loop: claim auto work, run one stage, write back."""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

from factory import obs, store, worker
from factory.db import connect


def _busy(conn: sqlite3.Connection, ticket_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runs WHERE ticket_id = ? AND status = 'running' LIMIT 1",
        (ticket_id,),
    ).fetchone()
    return row is not None


def _project(conn: sqlite3.Connection, project_id: str) -> dict:
    return dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())


def _worktree(repo: Path, ticket_id: str) -> Path:
    dest = repo.parent / ".worktrees" / ticket_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    branch = f"factory/{ticket_id}"
    subprocess.run(
        ["git", "worktree", "add", "-B", branch, str(dest), "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


def handle(conn: sqlite3.Connection, ticket: dict) -> None:
    tid = ticket["id"]
    state = ticket["state"]
    if _busy(conn, tid):
        return
    try:
        if state == "ready_to_plan":
            _plan(conn, ticket)
        elif state == "planning":
            _plan(conn, ticket, already=True)
        elif state == "implementing":
            _implement(conn, ticket)
        elif state == "validating":
            _validate(conn, ticket)
        elif state == "pr_open":
            store.transition(conn, tid, "merge_review", "runner")
        elif state == "integrating":
            store.transition(conn, tid, "done", "runner")
    except Exception as exc:
        obs.record_error(
            conn,
            source="runner",
            message=str(exc),
            detail=obs.format_exc(exc),
            ticket_id=tid,
        )
        try:
            store.transition(conn, tid, "failed", "runner")
        except Exception:
            pass


def _plan(conn: sqlite3.Connection, ticket: dict, already: bool = False) -> None:
    tid = ticket["id"]
    if not already:
        store.transition(conn, tid, "planning", "runner")
    run = store.start_run(conn, tid, "planning")
    proj = _project(conn, ticket["project_id"])
    cwd = Path(proj["repo_path"])
    brief = worker.plan_brief(ticket["title"], ticket["body"])
    result = worker.run(cwd, brief)
    store.finish_run(
        conn, run["id"], ok=result.ok, stdout=result.stdout, stderr=result.stderr
    )
    if not result.ok:
        obs.record_error(
            conn,
            source="worker",
            message="plan run failed",
            detail=result.stderr or result.stdout,
            ticket_id=tid,
            run_id=run["id"],
        )
        store.transition(conn, tid, "failed", "runner")
        return
    store.put_doc(conn, tid, "plan", result.stdout.strip(), "worker")
    store.transition(conn, tid, "plan_review", "runner")


def _implement(conn: sqlite3.Connection, ticket: dict) -> None:
    tid = ticket["id"]
    plan = store.latest_doc(conn, tid, "plan")
    if not plan:
        raise RuntimeError("no approved plan")
    run = store.start_run(conn, tid, "implementing")
    proj = _project(conn, ticket["project_id"])
    repo = Path(proj["repo_path"])
    cwd = _worktree(repo, tid)
    brief = worker.implement_brief(ticket["title"], ticket["body"], plan["body"])
    result = worker.run(cwd, brief)
    store.finish_run(
        conn, run["id"], ok=result.ok, stdout=result.stdout, stderr=result.stderr
    )
    store.put_doc(conn, tid, "result", result.stdout.strip() or result.stderr, "worker")
    if not result.ok:
        obs.record_error(
            conn,
            source="worker",
            message="implement run failed",
            detail=result.stderr or result.stdout,
            ticket_id=tid,
            run_id=run["id"],
        )
        store.transition(conn, tid, "failed", "runner")
        return
    store.transition(conn, tid, "validating", "runner")


def _validate(conn: sqlite3.Connection, ticket: dict) -> None:
    tid = ticket["id"]
    run = store.start_run(conn, tid, "validating")
    proj = _project(conn, ticket["project_id"])
    cmd = proj.get("validate_cmd") or "true"
    repo = Path(proj["repo_path"])
    wt = repo.parent / ".worktrees" / tid
    cwd = wt if wt.exists() else repo
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=120
        )
        ok = proc.returncode == 0
        store.finish_run(
            conn, run["id"], ok=ok, stdout=proc.stdout, stderr=proc.stderr
        )
        store.transition(conn, tid, "pr_open" if ok else "implementing", "runner")
        if not ok:
            obs.record_error(
                conn,
                source="runner",
                message="validate failed",
                detail=proc.stderr or proc.stdout,
                ticket_id=tid,
                run_id=run["id"],
                level="warn",
            )
    except Exception as exc:
        store.finish_run(conn, run["id"], ok=False, stdout="", stderr=str(exc))
        store.transition(conn, tid, "failed", "runner")
        raise


def tick(conn: sqlite3.Connection) -> bool:
    ticket = store.claim_auto(conn)
    if not ticket:
        return False
    handle(conn, ticket)
    return True


def loop(interval: float = 2.0) -> None:
    conn = connect()
    obs.emit("runner_start")
    while True:
        try:
            tick(conn)
        except Exception as exc:
            obs.record_error(conn, source="runner", message=str(exc), detail=obs.format_exc(exc))
        time.sleep(interval)


def main() -> None:
    loop()


if __name__ == "__main__":
    main()
