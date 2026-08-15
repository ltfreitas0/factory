"""HTTP control plane. Few routes; state changes go through store.transition."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from factory import obs, runner, sm, store
from factory.db import connect, rows
from factory.sm import IllegalTransition

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    playground = ROOT / "data" / "playground"
    _ensure_git_repo(playground)
    store.ensure_playground(conn, str(playground))
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
    project: str = "playground"


class TransitionIn(BaseModel):
    to: str
    actor: str = Field(default="human", pattern="^(human|runner)$")


class DocIn(BaseModel):
    kind: str
    body: str
    author: str = "human"


def _db():
    return app.state.db


def _ensure_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if (path / ".git").exists():
        return
    import subprocess

    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# playground\n\nFactory implementer target.\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=factory@local", "-c", "user.name=factory", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


@app.get("/api/health")
def health():
    conn = _db()
    err_n = conn.execute("SELECT COUNT(*) n FROM errors").fetchone()["n"]
    open_n = conn.execute(
        "SELECT COUNT(*) n FROM tickets WHERE state NOT IN ('done')"
    ).fetchone()["n"]
    return {"ok": True, "open_tickets": open_n, "errors": err_n}


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


def main() -> None:
    import uvicorn

    host = os.environ.get("FACTORY_API_HOST", "127.0.0.1")
    port = int(os.environ.get("FACTORY_API_PORT", "8051"))
    uvicorn.run("factory.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
