"""HTTP control plane assembly: app, lifespan, auth, routers.

Routes live in factory/routers/ (auth, projects, tickets, platform, files,
tape). This file is the thin entry: middleware, lifecycle, and registration.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from factory import auth, cost, feed, obs, project, runner, store
from factory.db import connect
from factory.routers import auth as auth_router
from factory.routers import files as files_router
from factory.routers import platform as platform_router
from factory.routers import projects as projects_router
from factory.routers import tape as tape_router
from factory.routers import tickets as tickets_router
from factory.routers._common import set_app

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    playground = ROOT / "data" / "playground"
    _ensure_git_repo(playground)
    store.ensure_playground(conn, str(playground))
    corpora = project.workspace_root() / "corpora"
    _ensure_git_repo(corpora, readme="# corpora\n")
    store.ensure_project(conn, "corpora", str(corpora), "scripts/validate")
    _seed_corpora(conn)
    for slug in ("playground", "corpora"):
        row = project.get(conn, slug)
        if row:
            project.seed_files(conn, row["id"])
    app.state.db = conn
    feed.set_sink(lambda item: store.append_feed(conn, item))
    feed.hydrate(store.load_feed(conn))
    try:
        n = cost.backfill(conn)
        if n:
            obs.emit("cost_backfill", runs=n)
    except Exception as exc:
        obs.emit("cost_backfill_fail", message=str(exc))
    if os.environ.get("FACTORY_EMBED_RUNNER", "1") != "0":
        t = Thread(target=runner.loop, daemon=True)
        t.start()
    obs.emit("api_start")
    yield
    conn.close()


app = FastAPI(title="factory", lifespan=lifespan)
set_app(app)

CORS_ORIGINS = [o.strip() for o in os.environ.get("FACTORY_CORS", "*").split(",") if o.strip()]


class BoardAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Preflight must reach CORS (outer). Never challenge OPTIONS.
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if (
            path in {"/api/health", "/auth/login"}
            or path.startswith("/auth/google")
            or path == "/api/auth/cloudflare/callback"
            or path.startswith("/ingest/")
            or not path.startswith("/api/")
        ):
            return await call_next(request)
        presented = request.headers.get("authorization")
        if presented and presented.lower().startswith("bearer "):
            presented = presented.split(" ", 1)[1].strip()
        else:
            presented = request.query_params.get("token")
        # Session token (multi-user) first; board token (local) falls back.
        uid = auth.resolve(presented)
        if uid:
            request.state.user_id = uid
            return await call_next(request)
        if not auth.check(presented):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


# Last add_middleware is outermost. CORS must wrap auth so 401s and
# preflight both carry Access-Control-Allow-Origin.
app.add_middleware(BoardAuth)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
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


app.include_router(auth_router.router)
app.include_router(projects_router.router)
app.include_router(tickets_router.router)
app.include_router(platform_router.router)
app.include_router(files_router.router)
app.include_router(tape_router.router)


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


def main() -> None:
    import uvicorn

    host = os.environ.get("FACTORY_API_HOST", "127.0.0.1")
    port = int(os.environ.get("FACTORY_API_PORT", "8051"))
    uvicorn.run("factory.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
