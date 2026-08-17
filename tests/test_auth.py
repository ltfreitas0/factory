"""Auth: session tokens, Google OAuth, users/connections routes."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from factory import auth
from factory.db import connect


# --- session tokens -------------------------------------------------------

def test_mint_resolve_roundtrip(monkeypatch):
    monkeypatch.setenv("FACTORY_SESSION_SECRET", "test-secret")
    token = auth.mint("usr_abc")
    assert auth.resolve(token) == "usr_abc"
    assert auth.resolve("garbage") is None
    assert auth.resolve(token.split(".")[0] + ".deadbeef") is None


def test_token_expiry(monkeypatch):
    monkeypatch.setenv("FACTORY_SESSION_SECRET", "test-secret")
    token = auth.mint("usr_abc", ttl=-10)
    assert auth.resolve(token) is None


def test_different_secret_rejects(monkeypatch):
    monkeypatch.setenv("FACTORY_SESSION_SECRET", "one")
    token = auth.mint("usr_abc")
    monkeypatch.setenv("FACTORY_SESSION_SECRET", "two")
    assert auth.resolve(token) is None


# --- users ----------------------------------------------------------------

def test_get_or_create_user(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    conn = connect()
    u = auth.get_or_create_user(conn, {"google_sub": "sub-1", "email": "a@b.c", "name": "Ann"})
    assert u["google_sub"] == "sub-1"
    again = auth.get_or_create_user(conn, {"google_sub": "sub-1", "email": "a@b.c", "name": "Ann"})
    assert again["id"] == u["id"]
    # profile refresh
    changed = auth.get_or_create_user(conn, {"google_sub": "sub-1", "email": "x@y.z", "name": "Ann"})
    assert changed["email"] == "x@y.z"
    assert changed["id"] == u["id"]
    conn.close()


def test_connections_crud(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    conn = connect()
    u = auth.get_or_create_user(conn, {"google_sub": "s", "email": "a@b", "name": "N"})
    c = auth.add_connection(conn, u["id"], provider="github", external_id="ltfreitas0")
    assert auth.list_connections(conn, u["id"])[0]["external_id"] == "ltfreitas0"
    assert auth.remove_connection(conn, u["id"], c["id"])
    assert auth.list_connections(conn, u["id"]) == []
    assert not auth.remove_connection(conn, u["id"], "missing")
    conn.close()


# --- fake Google + API routes ---------------------------------------------

class _FakeGoogle(BaseHTTPRequestHandler):
    backend = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        code = (form.get("code") or [""])[0]
        body = json.dumps(
            {
                "id_token": f"fake.{code}.sig",
                "access_token": f"at-{code}",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        body = json.dumps(
            {
                "sub": "sub-42",
                "email": "bix@henosis.cc",
                "name": "Bix",
                "picture": "",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_google(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGoogle)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:5510/auth/google/callback")
    monkeypatch.setattr(auth, "GOOGLE_TOKEN_URL", f"{base}/token")
    monkeypatch.setattr(auth, "GOOGLE_TOKENINFO_URL", f"{base}/tokeninfo")
    monkeypatch.setattr(auth, "GOOGLE_USERINFO_URL", f"{base}/userinfo")
    yield server
    server.shutdown()


from contextlib import contextmanager


@contextmanager
def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.delenv("FACTORY_AUTH_TOKEN", raising=False)
    from factory.api import app

    with TestClient(app) as client:
        yield client


def test_oauth_flow_and_me(fake_google, tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        # start → redirect to google
        r = client.get("/auth/google", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert "accounts.google.com" in r.headers["location"]
        # callback → redirect to SPA with session token
        state = auth.new_state()
        r = client.get(
            f"/auth/google/callback?code=xyz&state={state}", follow_redirects=False
        )
        assert r.status_code in (302, 307)
        loc = r.headers["location"]
        assert "/#/auth?token=" in loc
        token = loc.split("token=", 1)[1]
        assert auth.resolve(token) is not None
        # me with session token
        r = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == "bix@henosis.cc"
        assert data["id"] == auth.resolve(token)
        # me without token → 401
        assert client.get("/api/users/me").status_code == 401


def test_oauth_denied(fake_google, tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        r = client.get("/auth/google/callback?error=access_denied", follow_redirects=False)
        assert "/#/auth?err=" in r.headers["location"]


def test_oauth_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/auth/google").status_code == 400
