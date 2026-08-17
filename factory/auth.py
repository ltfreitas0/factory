"""Auth: board token (local) + multi-user Google OAuth sessions.

- `check(presented)` — legacy FACTORY_AUTH_TOKEN board auth (local dev).
- Google OAuth (GOOGLE_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI) → users table
  keyed by `google_sub`; a successful login mints an HMAC-signed session
  token carrying `user_id`. `resolve(token)` returns the user id or None.
- Every /api route resolves `request.state.user_id` when a session token is
  presented; unauthenticated board-token access stays open locally.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_google_pending: dict[str, dict] = {}  # state -> {at, next}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- board token (legacy) -------------------------------------------------

def required() -> bool:
    return bool(os.environ.get("FACTORY_AUTH_TOKEN"))


def check(presented: str | None) -> bool:
    expected = os.environ.get("FACTORY_AUTH_TOKEN")
    if not expected:
        return True
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


# --- session tokens -------------------------------------------------------

def _session_secret() -> bytes:
    raw = (
        os.environ.get("FACTORY_SESSION_SECRET")
        or os.environ.get("FACTORY_AUTH_TOKEN")
        or "factory-dev-session-secret"
    )
    return raw.encode()


def mint(user_id: str, *, ttl: int = 30 * 24 * 3600) -> str:
    """HMAC-signed session token: base64url(json).hex(sig)."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"uid": user_id, "exp": int(time.time()) + ttl}).encode()
    ).rstrip(b"=").decode()
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def resolve(token: str | None) -> str | None:
    """Return user_id if the token is a valid, unexpired session token."""
    if not token:
        return None
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    uid = data.get("uid")
    return str(uid) if uid else None


# --- Google OAuth ---------------------------------------------------------

def oauth_configured() -> bool:
    return bool(
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    )


def redirect_uri() -> str:
    return os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "http://localhost:5510/auth/google/callback"


def authorize_url(state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "online",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTH_URL}?{params}"


def _request_json(method: str, url: str, *, headers: dict | None = None, data: dict | None = None) -> dict:
    body = None
    hdrs = {"User-Agent": "factory", "Accept": "application/json", **(headers or {})}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise ValueError(f"google http {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"google unreachable: {exc.reason}") from exc
    if not payload:
        return {}
    return json.loads(payload)


def exchange(code: str) -> dict:
    """Swap the OAuth code for a verified Google identity."""
    secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or ""
    data = _request_json(
        "POST",
        GOOGLE_TOKEN_URL,
        data={
            "client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "",
            "client_secret": secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
        },
    )
    id_token = data.get("id_token")
    if not id_token:
        raise ValueError("google oauth did not return an id_token")
    claims = _request_json("GET", f"{GOOGLE_TOKENINFO_URL}?id_token={urllib.parse.quote(id_token)}")
    sub = claims.get("sub")
    if not sub:
        raise ValueError("google tokeninfo did not return sub")
    # fresh profile (name/email may have changed since id_token issuance)
    access_token = data.get("access_token") or ""
    profile: dict = {}
    if access_token:
        try:
            profile = _request_json(
                "GET", GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
        except ValueError:
            profile = {}
    return {
        "google_sub": sub,
        "email": profile.get("email") or claims.get("email") or "",
        "name": profile.get("name") or claims.get("name") or "",
        "picture": profile.get("picture") or "",
        "access_token": access_token,
    }


# --- users / connections --------------------------------------------------

def get_or_create_user(conn, identity: dict) -> dict:
    row = conn.execute(
        "SELECT * FROM users WHERE google_sub = ?", (identity["google_sub"],)
    ).fetchone()
    if row is None:
        uid = f"usr_{uuid.uuid4().hex[:10]}"
        conn.execute(
            """INSERT INTO users (id, google_sub, email, name, avatar_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid, identity["google_sub"], identity.get("email") or "",
             identity.get("name") or "", identity.get("picture") or "", _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    else:
        # refresh mutable profile fields
        email = identity.get("email") or row["email"]
        name = identity.get("name") or row["name"]
        picture = identity.get("picture") or row["avatar_url"]
        if (email, name, picture) != (row["email"], row["name"], row["avatar_url"]):
            conn.execute(
                "UPDATE users SET email = ?, name = ?, avatar_url = ? WHERE id = ?",
                (email, name, picture, row["id"]),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    return dict(row)


def user_view(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "email": row.get("email") or "",
        "name": row.get("name") or "",
        "avatar_url": row.get("avatar_url") or "",
        "default_provider_id": row.get("default_provider_id") or "",
    }


def get_user(conn, user_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def list_connections(conn, user_id: str) -> list[dict]:
    return [
        {
            "id": r["id"],
            "provider": r["provider"],
            "external_id": r["external_id"],
            "scopes": r["scopes"] or "",
            "cf_account_id": r["cf_account_id"] or "",
        }
        for r in conn.execute(
            "SELECT * FROM connections WHERE user_id = ? ORDER BY created_at", (user_id,)
        ).fetchall()
    ]


def add_connection(
    conn, user_id: str, *, provider: str, external_id: str = "", scopes: str = "",
    token_ref: str = "", cf_account_id: str = "",
) -> dict:
    cid = f"con_{uuid.uuid4().hex[:10]}"
    conn.execute(
        """INSERT INTO connections (id, user_id, provider, external_id, scopes, token_ref, cf_account_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, user_id, provider, external_id, scopes, token_ref, cf_account_id, _now()),
    )
    conn.commit()
    return {"id": cid, "provider": provider, "external_id": external_id}


def remove_connection(conn, user_id: str, connection_id: str) -> bool:
    cur = conn.execute(
        "DELETE FROM connections WHERE id = ? AND user_id = ?", (connection_id, user_id)
    )
    conn.commit()
    return cur.rowcount > 0


def set_default_provider(conn, user_id: str, provider_id: str) -> None:
    conn.execute(
        "UPDATE users SET default_provider_id = ? WHERE id = ?", (provider_id, user_id)
    )
    conn.commit()


# --- OAuth state tracking -------------------------------------------------

def new_state() -> str:
    state = secrets.token_urlsafe(16)
    _google_pending[state] = {"at": int(time.time())}
    return state


def pop_state(state: str) -> bool:
    entry = _google_pending.pop(state, None)
    if not entry:
        return False
    return int(time.time()) - entry["at"] < 600  # 10 min
