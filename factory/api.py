"""HTTP control plane. Few routes; state changes go through store.transition."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from factory import feed, obs, runner, sm, store
from factory.db import connect, rows
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
    store.ensure_project(conn, "corpora", str(corpora), "true")
    _seed_corpora(conn)
    app.state.db = conn
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


def _seed_corpora(conn) -> None:
    spec = Path("/home/bix/projects/corpora/.meta/readme.d")
    n = conn.execute(
        """SELECT COUNT(*) n FROM tickets t
           JOIN projects p ON p.id = t.project_id WHERE p.slug = 'corpora'"""
    ).fetchone()["n"]
    if n:
        return
    proj = conn.execute("SELECT * FROM projects WHERE slug = 'corpora'").fetchone()
    if not proj:
        return
    body = spec.read_text() if spec.exists() else "Build CORPORA v1."
    store.create_ticket(
        conn,
        project_id=proj["id"],
        title="CORPORA v1: agent-native kanban + auth + MCP",
        body=(
            "Build CORPORA in this repo. Source of truth for product intent:\n\n"
            f"{body}\n\n"
            "First vertical slice: authenticated kanban (columns, cards, assignees) "
            "plus an MCP surface agents can list/read/create/move cards. "
            "Keep v1 small. Add tests that prove an agent can move a card with "
            "valid auth and is rejected without it.\n\n"
            "Validation: a test command you add must pass; document it in README."
        ),
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


@app.get("/api/projects")
def projects():
    return rows(_db().execute("SELECT * FROM projects"))


@app.get("/api/tickets")
def tickets():
    return store.list_tickets(_db())


@app.post("/api/tickets")
def create_ticket(body: TicketIn):
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug = ?", (body.project,)).fetchone()
    if not proj:
        raise HTTPException(404, "unknown project")
    return store.create_ticket(conn, project_id=proj["id"], title=body.title, body=body.body)


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
    return do_transition(ticket_id, TransitionIn(to="ready_to_plan", actor="human"))


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


@app.get("/api/stream")
async def stream():
    q = feed.subscribe()

    async def gen():
        try:
            yield ": connected\n\n"
            for item in feed.history():
                yield f"data: {feed.dump(item)}\n\n"
            loop = asyncio.get_event_loop()
            while True:
                item = await loop.run_in_executor(None, q.get)
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
