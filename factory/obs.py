"""Structured logs + centralized error rows. The window into factory health."""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_path() -> Path:
    p = Path(os.environ.get("FACTORY_LOG", "var/logs/factory.jsonl"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def emit(event: str, **fields: Any) -> None:
    rec = {"at": _now(), "event": event, **fields}
    line = json.dumps(rec, default=str)
    with log_path().open("a") as f:
        f.write(line + "\n")


def record_error(
    conn,
    *,
    source: str,
    message: str,
    detail: str | None = None,
    ticket_id: str | None = None,
    run_id: str | None = None,
    level: str = "error",
) -> None:
    conn.execute(
        """INSERT INTO errors (source, level, message, detail, ticket_id, run_id, at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source, level, message, detail, ticket_id, run_id, _now()),
    )
    conn.commit()
    emit(
        "error",
        source=source,
        level=level,
        message=message,
        ticket_id=ticket_id,
        run_id=run_id,
    )


def format_exc(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
