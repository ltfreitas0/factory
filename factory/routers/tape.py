"""Tape: health, events, errors, states, cycle, feed, costs, SSE stream."""

from __future__ import annotations

import asyncio
import time
from queue import Empty

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from factory import cost, feed, sm, store
from factory.db import rows
from factory.routers._common import _project, db

router = APIRouter()


@router.get("/api/health")
def health():
    conn = db()
    err_n = conn.execute("SELECT COUNT(*) n FROM errors").fetchone()["n"]
    open_n = conn.execute(
        "SELECT COUNT(*) n FROM tickets WHERE state NOT IN ('done')"
    ).fetchone()["n"]
    active = store.active_ticket(conn)
    return {
        "ok": True,
        "open_tickets": open_n,
        "errors": err_n,
        "active": None
        if not active
        else {"id": active["id"], "state": active["state"], "title": active["title"]},
        "default_project": "corpora",
    }


@router.get("/api/errors")
def errors(limit: int = 100):
    return rows(
        db().execute("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,))
    )


@router.get("/api/events")
def events(limit: int = 100):
    return rows(
        db().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    )


@router.get("/api/projects/{slug}/events")
def project_events(slug: str, limit: int = 100):
    proj = _project(slug)
    return rows(
        db().execute(
            """SELECT e.* FROM events e
               WHERE e.ticket_id IN (SELECT id FROM tickets WHERE project_id = ?)
                  OR e.run_id IN (SELECT id FROM runs WHERE project_id = ?)
               ORDER BY e.id DESC LIMIT ?""",
            (proj["id"], proj["id"], limit),
        )
    )


@router.get("/api/states")
def states():
    return {"states": list(sm.STATES)}


@router.get("/api/cycle")
def cycle():
    active = store.active_ticket(db())
    return {
        "states": list(sm.STATES),
        "active": None
        if not active
        else {"id": active["id"], "state": active["state"], "title": active["title"]},
    }


@router.get("/api/feed")
def feed_history():
    return feed.history()


@router.get("/api/costs")
def costs(project: str = "corpora"):
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE slug = ?", (project,)).fetchone()
    if not proj:
        raise HTTPException(404, "unknown project")
    return cost.project_total(conn, proj["id"])


@router.get("/api/stream")
async def stream():
    q = feed.subscribe()

    def _snapshot() -> dict:
        active = store.active_ticket(db())
        return {
            "at": time.strftime("%H:%M:%S"),
            "kind": "snapshot",
            "text": (active["title"] if active else "idle"),
            "ticket_id": active["id"] if active else None,
            "state": active["state"] if active else None,
            "title": active["title"] if active else None,
        }

    async def gen():
        try:
            yield ": connected\n\n"
            yield f"data: {feed.dump(_snapshot())}\n\n"
            for item in feed.history():
                if item.get("kind") == "snapshot":
                    continue
                yield f"data: {feed.dump(item)}\n\n"
            loop = asyncio.get_event_loop()
            while True:
                try:
                    item = await loop.run_in_executor(None, lambda: q.get(timeout=15))
                except Empty:
                    yield ": ping\n\n"
                    continue
                yield f"data: {feed.dump(item)}\n\n"
        finally:
            feed.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
