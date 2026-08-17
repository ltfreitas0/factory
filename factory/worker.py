"""dsh headless adapter. One fat invocation per stage. No session resume.

Owns both halves of the dsh interface: running the process (`run`) and
tailing its session.jsonl.zstd to publish thinking/token/tool events
(`tail`, `emit_record`). Co-changes with the streaming UX, so they live in
one module.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from factory import feed

DSH_SESSIONS = Path.home() / ".dsh" / "sessions"


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


# --- dsh session tailing (streaming think/token/tool to the feed) ---------

def session_folder(cwd: Path) -> Path:
    slug = "--" + str(cwd.resolve()).lstrip("/").replace("/", "-") + "--"
    return DSH_SESSIONS / slug


def newest_session_file(cwd: Path, ticket_id: str | None) -> Path | None:
    dirs: list[Path] = []
    folder = session_folder(cwd)
    if folder.is_dir():
        dirs.append(folder)
    if ticket_id:
        for p in DSH_SESSIONS.glob(f"*{ticket_id}*"):
            if p.is_dir():
                dirs.append(p)
    files: list[Path] = []
    for d in dirs:
        files.extend(d.glob("*/session.jsonl.zstd"))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def _decode(data: bytes) -> str:
    try:
        import zstandard as zstd
    except ImportError:
        return ""

    dctx = zstd.ZstdDecompressor()
    out = []
    try:
        with dctx.stream_reader(io.BytesIO(data)) as reader:
            while True:
                try:
                    chunk = reader.read(65536)
                except zstd.ZstdError:
                    break
                if not chunk:
                    break
                out.append(chunk)
    except zstd.ZstdError:
        pass
    return b"".join(out).decode("utf-8", errors="replace")


def emit_record(obj: dict, ticket_id: str | None, run_id: str | None = None) -> None:
    typ = obj.get("type")
    data = obj.get("data") or {}
    if typ == "reasoning-chunks":
        for piece in data.get("texts") or []:
            if piece:
                feed.publish("think", piece, ticket_id=ticket_id, run_id=run_id)
        return
    if typ == "text-chunks":
        for piece in data.get("texts") or []:
            if piece:
                feed.publish("token", piece, ticket_id=ticket_id, run_id=run_id)
        return
    if typ == "tool/call":
        name = data.get("name") or "tool"
        args = (data.get("arguments") or "")[:160]
        feed.publish("tool", f"{name} {args}", ticket_id=ticket_id, run_id=run_id)
        return
    if typ == "tool/result":
        name = data.get("name") or "tool"
        result = (data.get("result") or data.get("output") or "")
        feed.publish("tool_result", f"{name} → {result[:200]}", ticket_id=ticket_id, run_id=run_id)
        return
    if typ in {"assistant/message", "step/end"}:
        usage = data.get("usage") or {}
        if usage:
            toks = sum(
                int(usage.get(k) or 0)
                for k in ("inputTokens", "outputTokens", "cacheReadTokens", "reasoningTokens")
            )
            feed.publish("usage", f"+{toks} tokens", ticket_id=ticket_id, run_id=run_id)


def tail(cwd: Path, ticket_id: str | None, stop: callable, timeout: float = 1200,
         run_id: str | None = None) -> None:
    deadline = time.time() + timeout
    path: Path | None = None
    seen = 0
    while time.time() < deadline and not stop():
        if path is None or not path.exists():
            path = newest_session_file(cwd, ticket_id)
            seen = 0
            if path is None:
                time.sleep(0.2)
                continue
        try:
            data = path.read_bytes()
        except OSError:
            time.sleep(0.1)
            continue
        text = _decode(data)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        for ln in lines[seen:]:
            try:
                emit_record(json.loads(ln), ticket_id, run_id=run_id)
            except json.JSONDecodeError:
                continue
        seen = len(lines)
        time.sleep(0.08)


def _pump(stream, chunks: list[str], kind: str, ticket_id: str | None, run_id: str | None = None) -> None:
    """Forward bytes as they arrive so the UI can show a live token feed."""
    assert stream is not None
    while True:
        piece = stream.read(64)
        if not piece:
            break
        chunks.append(piece)
        feed.publish(kind, piece, ticket_id=ticket_id, run_id=run_id)


def run(cwd: Path, brief: str, timeout: int = 600, ticket_id: str | None = None,
        run_id: str | None = None) -> WorkerResult:
    cmd = [_bin(), "--profile", "headless", brief]
    feed.publish("agent", f"$ {' '.join(cmd[:3])} …", ticket_id=ticket_id, run_id=run_id)
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
            target=_pump, args=(proc.stdout, out_chunks, "agent", ticket_id, run_id), daemon=True
        )
        t_err = threading.Thread(
            target=_pump, args=(proc.stderr, err_chunks, "stderr", ticket_id, run_id), daemon=True
        )
        t_out.start()
        t_err.start()
        stop = {"v": False}
        t_tr = threading.Thread(
            target=tail,
            args=(cwd, ticket_id, lambda: stop["v"], timeout + 5, run_id),
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
            feed.publish("stderr", "worker timeout", ticket_id=ticket_id, run_id=run_id)
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
        feed.publish("stderr", f"{_bin()} not on PATH", ticket_id=ticket_id, run_id=run_id)
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
