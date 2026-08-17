"""Cloudflare OAuth app flow: authorize URL, code exchange, finish, browser callback."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from factory import apps
from factory.db import connect
from factory.project import create


def test_cf_oauth_available_requires_creds(monkeypatch):
    monkeypatch.delenv("CF_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("CF_OAUTH_CLIENT_SECRET", raising=False)
    assert not apps.oauth_available("cloudflare")
    monkeypatch.setenv("CF_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("CF_OAUTH_CLIENT_SECRET", "csecret")
    assert apps.oauth_available("cloudflare")


def test_cf_oauth_start_builds_authorize_url(monkeypatch):
    monkeypatch.setenv("CF_OAUTH_CLIENT_ID", "cid-123")
    monkeypatch.setenv("CF_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("CF_OAUTH_REDIRECT_URI", "http://localhost:5510/api/auth/cloudflare/callback")
    out = apps.oauth_start("demo", "cloudflare")
    assert out["url"].startswith("https://dash.cloudflare.com/oauth2/auth?")
    q = parse_qs(urlparse(out["url"]).query)
    assert q["client_id"] == ["cid-123"]
    assert q["redirect_uri"] == ["http://localhost:5510/api/auth/cloudflare/callback"]
    assert q["response_type"] == ["code"]
    assert q["state"] == [out["state"]]
    assert "workers:write" in q["scope"][0]  # permissive scope set
    assert "pages:write" in q["scope"][0]


class _FakeCfOAuth(BaseHTTPRequestHandler):
    backend = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        code = (form.get("code") or [""])[0]
        if code != "good-code":
            body = json.dumps({"error": "invalid_grant"}).encode()
            self.send_response(400)
        else:
            body = json.dumps({"access_token": "cf-oauth-token", "scope": "workers:write"}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_cf_oauth(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCfOAuth)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("CF_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("CF_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(apps, "CF_OAUTH_TOKEN_URL", f"{base}/token")
    yield server
    server.shutdown()


def test_cf_oauth_finish_exchanges_and_installs(fake_cf_oauth, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("CF_OAUTH_REDIRECT_URI", raising=False)

    def fake_accounts(token):
        return [{"id": "acct-9", "name": "OAuth Acct"}]

    monkeypatch.setattr(apps, "cf_accounts", fake_accounts)
    conn = connect()
    create(conn, slug="demo", name="Demo")
    # stash a pending state the way oauth_start does
    state = apps.oauth_start("demo", "cloudflare")["state"]
    out = apps.oauth_finish(conn, "good-code", state)
    assert out == {"slug": "demo", "app": "cloudflare"}
    listed = apps.list_apps(conn, "demo" if False else _proj_id(conn))
    cf = next(a for a in listed if a["id"] == "cloudflare")
    assert cf["installed"]
    assert cf["identity"] == "OAuth Acct"
    conn.close()


def _proj_id(conn):
    return conn.execute("SELECT id FROM projects WHERE slug='demo'").fetchone()["id"]


def test_cf_oauth_bad_code_raises(fake_cf_oauth, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    conn = connect()
    state = apps.oauth_start("demo", "cloudflare")["state"]
    with pytest.raises(apps.AppError):
        apps.oauth_finish(conn, "bad-code", state)
    conn.close()


@contextmanager
def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.delenv("FACTORY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    from factory.api import app

    with TestClient(app) as client:
        yield client


def test_cf_oauth_browser_callback(fake_cf_oauth, tmp_path, monkeypatch):
    """GET /api/auth/cloudflare/callback exchanges and redirects to the SPA."""
    monkeypatch.setattr(apps, "cf_accounts", lambda token: [{"id": "acct-9", "name": "OAuth Acct"}])
    with _client(tmp_path, monkeypatch) as client:
        client.post("/api/projects", json={"name": "Demo", "slug": "demo"})
        state = apps.oauth_start("demo", "cloudflare")["state"]
        r = client.get(
            f"/api/auth/cloudflare/callback?code=good-code&state={state}",
            follow_redirects=False,
        )
        assert r.status_code in (302, 307)
        assert "/#/p/demo" in r.headers["location"]
        # app is now installed
        listed = client.get("/api/projects/demo/apps").json()
        cf = next(a for a in listed if a["id"] == "cloudflare")
        assert cf["installed"]


def test_cf_oauth_callback_error(fake_cf_oauth, tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        r = client.get("/api/auth/cloudflare/callback?error=access_denied", follow_redirects=False)
        assert "/#/" in r.headers["location"]
