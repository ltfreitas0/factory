"""HTTP control plane. Few routes; state changes go through store.transition."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty
from threading import Thread

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from factory import cost, feed, files, messages, obs, runner, sm, store
from factory.db import connect, rows
from factory.messages import AuthError
from factory.sm import IllegalTransition

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    playground = ROOT / "data" / "playground"
    _ensure_git_repo(playground)
    store.ensure_playground(conn, str(playground))
    corpora = Path("/home/bix/projects/corpora")
    _ensure_git_repo(corpora, readme="# corpora\n")
    store.ensure_project(conn, "corpora", str(corpora), "scripts/validate")
    _seed_corpora(conn)
    app.state.db = conn
    feed.set_sink(lambda item: store.append_feed(conn, item))
    feed.hydrate(store.load_feed(conn))
    try:
        n = cost.backfill(conn)
        if n:
            obs.emit("cost_backfill", runs=n)
    except Exception as exc:
        obs.emit("cost_backfill_fail", message=str(exc))
    t = Thread(target=runner.loop, daemon=True)
    t.start()
    obs.emit("api_start")
    yield
    conn.close()


app = FastAPI(title="factory", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def all_errors(request, exc):
    if isinstance(exc, HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    conn = getattr(request.app.state, "db", None)
    if conn is not None:
        obs.record_error(
            conn, source="api", message=str(exc), detail=obs.format_exc(exc)
        )
    obs.emit("api_unhandled", message=str(exc), path=str(request.url))
    return JSONResponse({"detail": "internal error"}, status_code=500)


class TicketIn(BaseModel):
    title: str
    body: str = ""
    project: str = "corpora"
    kind: str = "build"
    source: str = "human"


class TransitionIn(BaseModel):
    to: str
    actor: str = Field(default="human", pattern="^(human|runner)$")


class DocIn(BaseModel):
    kind: str
    body: str
    author: str = "human"


def _db():
    return app.state.db


def _ensure_git_repo(path: Path, readme: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if (path / ".git").exists():
        return
    import subprocess

    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    readme_path = path / "README.md"
    if not readme_path.exists():
        readme_path.write_text(readme or "# playground\n\nFactory implementer target.\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=path)
    if staged.returncode != 0:
        subprocess.run(
            ["git", "-c", "user.email=factory@local", "-c", "user.name=factory", "commit", "-m", "init"],
            cwd=path,
            check=True,
            capture_output=True,
        )


CORPORA_BRIEF = """Build CORPORA v1 in this repo. Product: agent-native kanban + auth + MCP.

Intent is in .meta/readme.d (executive summary only). CORPORA is the product —
not a second factory UI, not QM, not Slack.

v1 slice — keep it small:
1. Auth: simple bearer token. Unauthenticated writes are rejected.
2. Kanban: columns (todo / doing / done) and cards (title, body, assignee, column).
3. Human UI: one page, columns + cards. SQLite is fine.
4. MCP server: list / read / create / move cards. Same auth as the HTTP API.
5. Tests that prove: a caller can move a card with a valid token, and is rejected without one.

Stack: Python FastAPI + SQLite + a tiny HTML or React UI, or bun + Hono. Pick one and stay tiny.

Validation: you MUST add an executable scripts/validate that runs the test suite and exits 0. Document how to run the app and the tests in README.

Do not implement Slack, QM, multiplayer harness, crons, or cloud deploy.
"""


def _seed_corpora(conn) -> None:
    proj = conn.execute("SELECT * FROM projects WHERE slug = 'corpora'").fetchone()
    if not proj:
        return
    existing = conn.execute(
        """SELECT t.id, t.state FROM tickets t
           WHERE t.project_id = ? AND t.title LIKE 'CORPORA v1%'
           ORDER BY t.created_at ASC LIMIT 1""",
        (proj["id"],),
    ).fetchone()
    if existing:
        if existing["state"] == "inbox":
            conn.execute(
                "UPDATE tickets SET body = ? WHERE id = ?",
                (CORPORA_BRIEF, existing["id"]),
            )
            conn.commit()
        return
    store.create_ticket(
        conn,
        project_id=proj["id"],
        title="CORPORA v1: agent-native kanban + auth + MCP",
        body=CORPORA_BRIEF,
    )


@app.get("/api/health")
def health():
    conn = _db()
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


def _project(slug: str):
    row = _db().execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown project")
    return dict(row)


@app.get("/api/projects")
def projects():
    return rows(_db().execute("SELECT * FROM projects"))


@app.post("/api/projects/{slug}/ingest-token")
def mint_ingest(slug: str):
    """Rotate the project's only ingest token. Plaintext is returned once."""
    proj = _project(slug)
    token = messages.rotate_ingest_token(_db(), proj["id"])
    obs.emit("ingest_token_rotated", project=slug)
    return {"token": token, "once": True}


class IngestIn(BaseModel):
    source: str = "app"
    payload: dict = Field(default_factory=dict)


@app.post("/ingest/{slug}/messages")
def ingest_message(slug: str, body: IngestIn, authorization: str | None = Header(default=None)):
    proj = _project(slug)
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    try:
        msg = messages.ingest(_db(), proj["id"], token, body.source, body.payload)
    except AuthError:
        raise HTTPException(401, "invalid ingest token") from None
    import json as _json

    wf = []
    raw = proj.get("workflow")
    if raw:
        try:
            wf = _json.loads(raw)
        except _json.JSONDecodeError:
            wf = []
    if not wf:
        wf = [
            {"id": "inbox", "kind": "human"},
            {"id": "build", "kind": "agent"},
        ]
    processed = messages.process(_db(), proj["id"], msg["id"], wf)
    obs.emit(
        "message_ingested",
        project=slug,
        message_id=msg["id"],
        source=body.source,
        dropped=bool((processed.get("result") or {}).get("drop")),
    )
    return processed


@app.get("/api/projects/{slug}/messages")
def project_messages(slug: str):
    return messages.list_messages(_db(), _project(slug)["id"])


@app.put("/api/projects/{slug}/files/{path:path}")
def put_file(slug: str, path: str, body: dict):
    proj = _project(slug)
    store_name = body.get("store") or "repo"
    try:
        return files.put(_db(), proj["id"], path, body.get("body") or "", store=store_name)
    except files.FileError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{slug}/files/{path:path}")
def get_file(slug: str, path: str, store: str = "repo"):
    proj = _project(slug)
    try:
        return files.get(_db(), proj["id"], path, store=store)
    except files.FileError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/tickets")
def tickets(project: str | None = None):
    return store.list_tickets(_db(), project=project)


@app.post("/api/tickets")
def create_ticket(body: TicketIn):
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug = ?", (body.project,)).fetchone()
    if not proj:
        raise HTTPException(404, "unknown project")
    return store.create_ticket(
        conn,
        project_id=proj["id"],
        title=body.title,
        body=body.body,
        kind=body.kind,
        source=body.source,
    )


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    t = store.get_ticket(_db(), ticket_id)
    if not t:
        raise HTTPException(404, "not found")
    return t


@app.post("/api/tickets/{ticket_id}/transition")
def do_transition(ticket_id: str, body: TransitionIn):
    try:
        return store.transition(_db(), ticket_id, body.to, body.actor)
    except IllegalTransition as e:
        raise HTTPException(409, str(e)) from e
    except KeyError:
        raise HTTPException(404, "not found") from None


@app.post("/api/tickets/{ticket_id}/accept")
def accept(ticket_id: str):
    t = store.get_ticket(_db(), ticket_id)
    if not t:
        raise HTTPException(404, "not found")
    dest = "ready_to_validate" if t.get("kind") == "validate" else "ready_to_plan"
    if t["state"] not in ("inbox", "proposed"):
        raise HTTPException(409, f"cannot accept from {t['state']}")
    return do_transition(ticket_id, TransitionIn(to=dest, actor="human"))


@app.post("/api/tickets/{ticket_id}/approve-plan")
def approve_plan(ticket_id: str):
    t = store.get_ticket(_db(), ticket_id)
    if not t:
        raise HTTPException(404, "not found")
    if not store.latest_doc(_db(), ticket_id, "plan"):
        raise HTTPException(409, "no plan to approve")
    return store.transition(_db(), ticket_id, "implementing", "human")


@app.post("/api/tickets/{ticket_id}/approve-merge")
def approve_merge(ticket_id: str):
    return do_transition(ticket_id, TransitionIn(to="integrating", actor="human"))


@app.post("/api/tickets/{ticket_id}/documents")
def write_doc(ticket_id: str, body: DocIn):
    if not store.get_ticket(_db(), ticket_id):
        raise HTTPException(404, "not found")
    return store.put_doc(_db(), ticket_id, body.kind, body.body, body.author)


@app.get("/api/errors")
def errors(limit: int = 100):
    return rows(
        _db().execute("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,))
    )


@app.get("/api/events")
def events(limit: int = 100):
    return rows(
        _db().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    )


@app.get("/api/states")
def states():
    return {"states": list(sm.STATES)}


@app.get("/api/cycle")
def cycle():
    active = store.active_ticket(_db())
    return {
        "states": list(sm.STATES),
        "active": None
        if not active
        else {"id": active["id"], "state": active["state"], "title": active["title"]},
    }


@app.get("/api/feed")
def feed_history():
    return feed.history()


@app.get("/api/costs")
def costs(project: str = "corpora"):
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug = ?", (project,)).fetchone()
    if not proj:
        raise HTTPException(404, "unknown project")
    return cost.project_total(conn, proj["id"])


@app.get("/api/stream")
async def stream():
    q = feed.subscribe()

    def _snapshot() -> dict:
        active = store.active_ticket(_db())
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


def main() -> None:
    import uvicorn

    host = os.environ.get("FACTORY_API_HOST", "127.0.0.1")
    port = int(os.environ.get("FACTORY_API_PORT", "8051"))
    uvicorn.run("factory.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
