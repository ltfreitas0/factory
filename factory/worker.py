"""dsh headless adapter. One fat invocation per stage. No session resume."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


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


def run(cwd: Path, brief: str, timeout: int = 600) -> WorkerResult:
    cmd = [_bin(), "--profile", "headless", brief]
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return WorkerResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            code=proc.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        return WorkerResult(ok=False, stdout=exc.stdout or "", stderr="timeout", code=124)
    except FileNotFoundError:
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
        "Do not git commit unless asked. When done, print a 5-line summary of what you changed."
    )
