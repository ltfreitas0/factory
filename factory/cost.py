"""Estimate run cost from dsh session traces. Rates are labeled estimates."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

# USD per million tokens. Override with FACTORY_RATES_JSON.
# deepseek-v4-flash via OpenCode Go — not billed as a published card; these are guesses.
DEFAULT_RATES = {
    "input": 0.14,
    "output": 0.28,
    "cache_read": 0.014,
    "reasoning": 0.28,
}

DSH_SESSIONS = Path.home() / ".dsh" / "sessions"


def rates() -> dict[str, float]:
    raw = os.environ.get("FACTORY_RATES_JSON")
    if raw:
        try:
            return {**DEFAULT_RATES, **json.loads(raw)}
        except json.JSONDecodeError:
            pass
    return dict(DEFAULT_RATES)


def usd(usage: dict, r: dict[str, float] | None = None) -> float:
    r = r or rates()
    return (
        usage.get("input", 0) * r["input"]
        + usage.get("output", 0) * r["output"]
        + usage.get("cache_read", 0) * r["cache_read"]
        + usage.get("reasoning", 0) * r["reasoning"]
    ) / 1_000_000


def _parse_session(path: Path) -> dict | None:
    try:
        import zstandard as zstd
    except ImportError:
        return None
    try:
        dctx = zstd.ZstdDecompressor()
        with path.open("rb") as f:
            with dctx.stream_reader(f) as reader:
                raw = reader.read()
    except Exception:
        return None
    inp = out = cache = reas = 0
    n = 0
    sid = path.parent.name
    for ln in raw.decode("utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "session":
            sid = o.get("id") or sid
        if o.get("type") != "assistant/message":
            continue
        u = (o.get("data") or {}).get("usage") or {}
        if not u:
            continue
        n += 1
        inp += int(u.get("inputTokens") or 0)
        out += int(u.get("outputTokens") or 0)
        cache += int(u.get("cacheReadTokens") or 0)
        reas += int(u.get("reasoningTokens") or 0)
    if not n:
        return None
    usage = {"input": inp, "output": out, "cache_read": cache, "reasoning": reas, "steps": n}
    return {"session_id": sid, "usage": usage, "tokens": inp + out + reas, "usd": usd(usage)}


def find_session(ticket_id: str, started_at: str | None = None) -> Path | None:
    """Newest session whose folder mentions the ticket, else corpora cwd sessions in the window."""
    if not DSH_SESSIONS.is_dir():
        return None
    cands: list[Path] = []
    for root, _dirs, files in os.walk(DSH_SESSIONS):
        for name in files:
            if name != "session.jsonl.zstd":
                continue
            p = Path(root) / name
            if ticket_id in str(p):
                cands.append(p)
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not started_at:
        return cands[0]
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return cands[0]
    for p in cands:
        # session written during or shortly after the run
        if p.stat().st_mtime >= start - 5:
            return p
    return cands[0]


def attach_run(conn, run_id: str, ticket_id: str, started_at: str | None = None) -> dict | None:
    path = find_session(ticket_id, started_at)
    if not path:
        return None
    parsed = _parse_session(path)
    if not parsed:
        return None
    conn.execute(
        "UPDATE runs SET tokens = ?, session_id = ?, usage_json = ? WHERE id = ?",
        (
            parsed["tokens"],
            parsed["session_id"],
            json.dumps(parsed["usage"]),
            run_id,
        ),
    )
    conn.commit()
    return parsed


def backfill(conn) -> int:
    n = 0
    rows = conn.execute(
        """SELECT id, ticket_id, started_at FROM runs
           WHERE status != 'running' AND (tokens IS NULL OR usage_json IS NULL)"""
    ).fetchall()
    for r in rows:
        if attach_run(conn, r["id"], r["ticket_id"], r["started_at"]):
            n += 1
    return n


def ticket_rollups(conn) -> dict[str, dict]:
    r = rates()
    out: dict[str, dict] = {}
    for row in conn.execute(
        """SELECT ticket_id, COALESCE(SUM(tokens), 0) tokens, usage_json
           FROM runs GROUP BY ticket_id"""
    ):
        usage = {"input": 0, "output": 0, "cache_read": 0, "reasoning": 0}
        # sum usage_json per run
        for urow in conn.execute(
            "SELECT usage_json FROM runs WHERE ticket_id = ? AND usage_json IS NOT NULL",
            (row["ticket_id"],),
        ):
            try:
                u = json.loads(urow["usage_json"])
            except json.JSONDecodeError:
                continue
            for k in usage:
                usage[k] += int(u.get(k) or 0)
        out[row["ticket_id"]] = {
            "tokens": int(row["tokens"] or 0),
            "usd": usd(usage, r),
            "usage": usage,
        }
    return out


def project_total(conn, project_id: str) -> dict:
    rolls = ticket_rollups(conn)
    ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM tickets WHERE project_id = ?", (project_id,)).fetchall()
    ]
    tokens = 0
    dollars = 0.0
    for tid in ids:
        part = rolls.get(tid) or {}
        tokens += int(part.get("tokens") or 0)
        dollars += float(part.get("usd") or 0)
    return {"tokens": tokens, "usd": dollars, "rates": rates(), "estimate": True}
