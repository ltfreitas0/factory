"""dsh headless adapter. One fat invocation per stage. No session resume."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from factory import feed, trace


@dataclass
class WorkerResult:
    ok: bool
    stdout: str
    stderr: str
    code: int


def _bin() -> str:
    return os.environ.get("DSH_BIN", "dsh")


def available() -> bool:
    return shutil.which(_bin()) is not None


def _pump(stream, chunks: list[str], kind: str, ticket_id: str | None) -> None:
    """Forward bytes as they arrive so the UI can show a live token feed."""
    assert stream is not None
    while True:
        piece = stream.read(64)
        if not piece:
            break
        chunks.append(piece)
        feed.publish(kind, piece, ticket_id=ticket_id)


def run(cwd: Path, brief: str, timeout: int = 600, ticket_id: str | None = None) -> WorkerResult:
    cmd = [_bin(), "--profile", "headless", brief]
    feed.publish("agent", f"$ {' '.join(cmd[:3])} …", ticket_id=ticket_id)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("NODE_DISABLE_COLORS", "1")
    env.setdefault("CI", "1")
    env.setdefault("PLAYWRIGHT_HEADLESS", "1")
    env["HEADLESS"] = "1"
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0,
            env=env,
        )
        out_chunks: list[str] = []
        err_chunks: list[str] = []

        t_out = threading.Thread(
            target=_pump, args=(proc.stdout, out_chunks, "agent", ticket_id), daemon=True
        )
        t_err = threading.Thread(
            target=_pump, args=(proc.stderr, err_chunks, "stderr", ticket_id), daemon=True
        )
        t_out.start()
        t_err.start()
        stop = {"v": False}
        t_tr = threading.Thread(
            target=trace.tail,
            args=(cwd, ticket_id, lambda: stop["v"], timeout + 5),
            daemon=True,
        )
        t_tr.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop["v"] = True
            proc.kill()
            proc.wait()
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            t_tr.join(timeout=2)
            feed.publish("stderr", "worker timeout", ticket_id=ticket_id)
            return WorkerResult(ok=False, stdout="".join(out_chunks), stderr="timeout", code=124)
        stop["v"] = True
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        t_tr.join(timeout=2)
        return WorkerResult(
            ok=code == 0,
            stdout="".join(out_chunks),
            stderr="".join(err_chunks),
            code=code,
        )
    except FileNotFoundError:
        feed.publish("stderr", f"{_bin()} not on PATH", ticket_id=ticket_id)
        return WorkerResult(ok=False, stdout="", stderr=f"{_bin()} not on PATH", code=127)


def plan_brief(title: str, body: str) -> str:
    return (
        "You are a factory planner. Do not edit files. Do not run tools unless you need to read the repo.\n"
        f"Ticket title: {title}\n"
        f"Ticket body:\n{body}\n\n"
        "Write a short implementation plan: steps, files to touch, how to verify. "
        "Plain markdown. No preamble."
    )


def implement_brief(title: str, body: str, plan: str) -> str:
    return (
        "You are a factory implementer. Execute the approved plan in this checkout.\n"
        f"Ticket: {title}\n\n{body}\n\n"
        f"Approved plan:\n{plan}\n\n"
        "Make the smallest change that satisfies the plan. "
        "Add or update tests so scripts/validate exits 0 (create that script if missing). "
        "Never launch a visible browser. Browser tests must use Playwright headless "
        "(CI=1, PLAYWRIGHT_HEADLESS=1, launch({ headless: true })). Prefer vitest + happy-dom "
        "for unit tests. When done, print a short summary of files changed."
    )
