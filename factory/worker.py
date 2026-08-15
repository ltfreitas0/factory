"""dsh headless adapter. One fat invocation per stage. No session resume."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from factory import feed


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


def run(cwd: Path, brief: str, timeout: int = 600, ticket_id: str | None = None) -> WorkerResult:
    cmd = [_bin(), "--profile", "headless", brief]
    feed.publish("agent", f"$ {' '.join(cmd[:3])} …", ticket_id=ticket_id)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        out_chunks: list[str] = []
        err_chunks: list[str] = []

        def pump(stream, chunks, kind: str) -> None:
            assert stream is not None
            for line in stream:
                chunks.append(line)
                text = line.rstrip("\n")
                if text:
                    feed.publish(kind, text, ticket_id=ticket_id)

        t_out = threading.Thread(target=pump, args=(proc.stdout, out_chunks, "agent"), daemon=True)
        t_err = threading.Thread(target=pump, args=(proc.stderr, err_chunks, "stderr"), daemon=True)
        t_out.start()
        t_err.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            feed.publish("stderr", "worker timeout", ticket_id=ticket_id)
            return WorkerResult(ok=False, stdout="".join(out_chunks), stderr="timeout", code=124)
        t_out.join(timeout=2)
        t_err.join(timeout=2)
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
        "Add or update tests so the project's validate command passes. "
        "When done, print a short summary of files changed."
    )
