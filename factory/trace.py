"""Tail dsh session.jsonl.zstd and publish thinking + content tokens."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

from factory import feed

DSH_SESSIONS = Path.home() / ".dsh" / "sessions"


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


def emit_record(obj: dict, ticket_id: str | None) -> None:
    typ = obj.get("type")
    data = obj.get("data") or {}
    if typ == "reasoning-chunks":
        for piece in data.get("texts") or []:
            if piece:
                feed.publish("think", piece, ticket_id=ticket_id)
        return
    if typ == "text-chunks":
        for piece in data.get("texts") or []:
            if piece:
                feed.publish("token", piece, ticket_id=ticket_id)
        return
    if typ == "tool/call":
        name = data.get("name") or "tool"
        args = (data.get("arguments") or "")[:160]
        feed.publish("tool", f"{name} {args}", ticket_id=ticket_id)


def tail(cwd: Path, ticket_id: str | None, stop: callable, timeout: float = 1200) -> None:
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
                emit_record(json.loads(ln), ticket_id)
            except json.JSONDecodeError:
                continue
        seen = len(lines)
        time.sleep(0.08)
