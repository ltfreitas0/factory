"""Identity: board login, Google OAuth, users, connections."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from factory import auth, obs
from factory.routers._common import _me, db

router = APIRouter()


class LoginIn(BaseModel):
    token: str = ""


@router.post("/auth/login")
def login(body: LoginIn):
    if not auth.check(body.token or None):
        raise HTTPException(401, "unauthorized")
    return {"token": body.token or "", "ok": True}


@router.get("/auth/google")
def google_start(next: str = ""):
    if not auth.oauth_configured():
        raise HTTPException(400, "google oauth not configured")
    state = auth.new_state()
    if next:
        auth._google_pending[state]["next"] = next[:500]
    from fastapi.responses import RedirectResponse

    return RedirectResponse(auth.authorize_url(state))


@router.get("/auth/google/callback")
def google_callback(code: str = "", state: str = "", error: str = ""):
    from fastapi.responses import RedirectResponse

    if error or not code or not auth.pop_state(state):
        return RedirectResponse("/#/auth?err=denied")
    try:
        identity = auth.exchange(code)
        conn = db()
        user = auth.get_or_create_user(conn, identity)
        token = auth.mint(user["id"])
    except ValueError as exc:
        return RedirectResponse(f"/#/auth?err={str(exc)[:120]}")
    return RedirectResponse(f"/#/auth?token={token}")


@router.get("/api/users/me")
def me(request: Request):
    conn, user = _me(request)
    return {
        **auth.user_view(user),
        "connections": auth.list_connections(conn, user["id"]),
    }


class MePatch(BaseModel):
    default_provider_id: str = ""


@router.patch("/api/users/me")
def patch_me(body: MePatch, request: Request):
    conn, user = _me(request)
    auth.set_default_provider(conn, user["id"], body.default_provider_id.strip())
    return auth.user_view(auth.get_user(conn, user["id"]))


@router.get("/api/users/me/connections")
def my_connections(request: Request):
    conn, user = _me(request)
    return auth.list_connections(conn, user["id"])


class ConnectionIn(BaseModel):
    provider: str
    external_id: str = ""
    scopes: str = ""
    token_ref: str = ""
    cf_account_id: str = ""


@router.post("/api/users/me/connections")
def add_connection(body: ConnectionIn, request: Request):
    conn, user = _me(request)
    if body.provider not in {"google", "github", "cloudflare"}:
        raise HTTPException(400, "bad provider")
    return auth.add_connection(
        conn,
        user["id"],
        provider=body.provider,
        external_id=body.external_id,
        scopes=body.scopes,
        token_ref=body.token_ref,
        cf_account_id=body.cf_account_id,
    )


@router.delete("/api/users/me/connections/{connection_id}")
def remove_connection(connection_id: str, request: Request):
    conn, user = _me(request)
    if not auth.remove_connection(conn, user["id"], connection_id):
        raise HTTPException(404, "not found")
    return {"ok": True}


@router.get("/api/auth/cloudflare/callback")
def cloudflare_oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Browser redirect target for the CF OAuth app (registered redirect URI)."""
    from fastapi.responses import RedirectResponse

    if error or not code:
        return RedirectResponse("/#/")
    try:
        out = _finish_cf(code, state)
    except Exception as exc:
        obs.emit("cf_oauth_fail", message=str(exc))
        return RedirectResponse("/#/")
    obs.emit("app_installed_oauth", project=out["slug"], app="cloudflare")
    return RedirectResponse(f"/#/p/{out['slug']}")


def _finish_cf(code: str, state: str):
    from factory import apps

    return apps.oauth_finish(db(), code, state)
