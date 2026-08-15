"""Local dispatcher. Dumb loop: claim auto work, run one stage, write back."""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

from factory import cost, feed, obs, store, worker
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
        elif state == "ready_to_validate":
            store.transition(conn, tid, "validating", "runner")
            ticket = store.get_ticket(conn, tid) or ticket
            _validate(conn, ticket)
        elif state == "validating":
            _validate(conn, ticket)
        elif state == "pr_open":
            store.transition(conn, tid, "merge_review", "runner")
        elif state == "integrating":
            _integrate(conn, ticket)
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
    feed.publish("runner", "planning…", ticket_id=tid, state="planning")
    result = worker.run(cwd, brief, ticket_id=tid)
    store.finish_run(
        conn, run["id"], ok=result.ok, stdout=result.stdout, stderr=result.stderr
    )
    cost.attach_run(conn, run["id"], tid, run.get("started_at"))
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
    steer = store.latest_doc(conn, tid, "steer")
    plan_body = plan["body"]
    if steer:
        plan_body += "\n\nHuman steer (must follow):\n" + steer["body"]
    run = store.start_run(conn, tid, "implementing")
    proj = _project(conn, ticket["project_id"])
    repo = Path(proj["repo_path"])
    cwd = _worktree(repo, tid)
    brief = worker.implement_brief(ticket["title"], ticket["body"], plan_body)
    feed.publish("runner", "implementing in worktree…", ticket_id=tid, state="implementing")
    result = worker.run(cwd, brief, timeout=1200, ticket_id=tid)
    blob = (result.stderr or "") + (result.stdout or "")
    if (not result.ok) and "STREAM_CLOSED" in blob:
        feed.publish("runner", "dsh STREAM_CLOSED — retrying once", ticket_id=tid)
        store.finish_run(
            conn, run["id"], ok=False, stdout=result.stdout, stderr=result.stderr
        )
        run = store.start_run(conn, tid, "implementing")
        result = worker.run(cwd, brief, timeout=1200, ticket_id=tid)
    store.finish_run(
        conn, run["id"], ok=result.ok, stdout=result.stdout, stderr=result.stderr
    )
    cost.attach_run(conn, run["id"], tid, run.get("started_at"))
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
    feed.publish("runner", f"validate: {cmd}", ticket_id=tid, state="validating")
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=120
        )
        ok = proc.returncode == 0
        store.finish_run(
            conn, run["id"], ok=ok, stdout=proc.stdout, stderr=proc.stderr
        )
        kind = ticket.get("kind") or "build"
        if ok:
            nxt = "done" if kind == "validate" else "pr_open"
        else:
            nxt = "failed" if kind == "validate" else "implementing"
        store.transition(conn, tid, nxt, "runner")
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


def _integrate(conn: sqlite3.Connection, ticket: dict) -> None:
    tid = ticket["id"]
    run = store.start_run(conn, tid, "integrating")
    proj = _project(conn, ticket["project_id"])
    repo = Path(proj["repo_path"])
    wt = repo.parent / ".worktrees" / tid
    branch = f"factory/{tid}"
    git_env = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "factory",
        "GIT_AUTHOR_EMAIL": "factory@local",
        "GIT_COMMITTER_NAME": "factory",
        "GIT_COMMITTER_EMAIL": "factory@local",
    }

    def git(args, cwd=repo):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, env=git_env
        )

    try:
        if wt.exists():
            st = git(["status", "--porcelain"], cwd=wt)
            if st.stdout.strip():
                git(["add", "-A"], cwd=wt)
                git(["commit", "-m", f"factory: {ticket['title']}"], cwd=wt)
        merge = git(["merge", "--no-ff", "-m", f"factory merge {tid}", branch])
        ok = merge.returncode == 0
        store.finish_run(
            conn, run["id"], ok=ok, stdout=merge.stdout, stderr=merge.stderr
        )
        if not ok:
            obs.record_error(
                conn,
                source="runner",
                message="integrate merge failed",
                detail=merge.stderr or merge.stdout,
                ticket_id=tid,
                run_id=run["id"],
            )
            store.transition(conn, tid, "failed", "runner")
            return
        store.transition(conn, tid, "done", "runner")
        spawned = store.spawn_from_repo(
            conn, project_id=ticket["project_id"], repo=repo, parent_id=tid
        )
        if spawned:
            feed.publish(
                "runner",
                f"spawned {len(spawned)} ticket(s) for review",
                ticket_id=tid,
            )
    except Exception:
        store.finish_run(conn, run["id"], ok=False, stdout="", stderr="integrate crashed")
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
