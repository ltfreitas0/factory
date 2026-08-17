"""Shared router helpers.

`set_app` is called once from factory.api after the FastAPI app is built;
`db()` returns the lifespan connection from `app.state.db`. Keeping the
handler bodies verbatim in the routers (only the decorator changes) is what
makes this split mechanical and safe.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, Request

_APP = None


def set_app(app) -> None:
    global _APP
    _APP = app


def db():
    return _APP.state.db


def _id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(slug: str) -> dict:
    row = db().execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown project")
    return dict(row)


def _me(request: Request):
    from factory import auth

    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(401, "unauthorized")
    user = auth.get_user(db(), uid)
    if not user:
        raise HTTPException(401, "no such user")
    return db(), user
