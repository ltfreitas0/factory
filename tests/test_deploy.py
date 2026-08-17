"""Pages direct-upload deploy: build → upload-token → assets → deployment."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from factory import deploy, files, sandbox
from factory.db import connect


class _FakeCF(BaseHTTPRequestHandler):
    backend = None

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "/upload-token" in self.path:
            return self._json({"success": True, "result": {"jwt": "fake-jwt"}})
        return self._json({"success": False, "errors": [{"message": "unexpected GET"}]}, 404)

    def do_POST(self):
        if self.path.endswith("/pages/assets/upload"):
            self.backend["assets"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            return self._json({"success": True, "result": None})
        if self.path.endswith("/pages/assets/upsert-hashes"):
            self.backend["upserted"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            return self._json({"success": True, "result": None})
        if "/deployments" in self.path:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            self.backend["deployment_body"] = raw
            return self._json(
                {
                    "success": True,
                    "result": {
                        "id": "dep-123",
                        "url": "https://factory-todo.pages.dev",
                        "environment": "production",
                    },
                }
            )
        return self._json({"success": False, "errors": [{"message": "unexpected POST"}]}, 404)


@pytest.fixture()
def fake_cf(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCF)
    _FakeCF.backend = {"assets": None, "deployment_body": None}
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(deploy, "CF_API", base)
    yield _FakeCF.backend, server
    server.shutdown()


def test_build_in_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "exec", lambda slug, cmd, timeout=None: {
        "ok": True, "stdout": "built", "stderr": "", "exitCode": 0,
    })
    out = deploy.build_in_sandbox("smoke", "bun run build")
    assert out["ok"] and "built" in out["stdout"]


def test_pull_dist(monkeypatch):
    monkeypatch.setattr(sandbox, "exec", lambda slug, cmd, timeout=None: {
        "ok": True, "stdout": "dist/index.html\ndist/assets/app.js\n", "stderr": "", "exitCode": 0,
    })
    monkeypatch.setattr(
        sandbox,
        "read_file",
        lambda slug, path: b"<html>" if path.endswith("index.html") else b"js()",
    )
    out = deploy.pull_dist("smoke")
    assert out == {"index.html": b"<html>", "assets/app.js": b"js()"}


def test_pull_dist_empty(monkeypatch):
    monkeypatch.setattr(sandbox, "exec", lambda slug, cmd, timeout=None: {
        "ok": True, "stdout": "", "stderr": "", "exitCode": 0,
    })
    with pytest.raises(deploy.DeployError):
        deploy.pull_dist("smoke")


def test_create_deployment_uploads_and_manifests(fake_cf):
    backend, _ = fake_cf
    files_map = {"index.html": b"<html>todo</html>", "assets/app.js": b"console.log(1)"}
    dep = deploy.create_deployment("acct-1", "factory-todo", "tok", files_map)
    assert dep["url"] == "https://factory-todo.pages.dev"
    # assets uploaded with Cloudflare's blake3(base64+ext) content keys
    assets = backend["assets"]
    assert len(assets) == 2
    keys = {a["key"] for a in assets}
    assert deploy._content_hash(b"<html>todo</html>", "index.html") in keys
    # hashes registered so the deployment can serve them
    assert set(backend["upserted"]["hashes"]) == keys
    # deployment body is multipart containing the manifest with /-prefixed keys
    body = backend["deployment_body"]
    assert '"/index.html"' in body and deploy._content_hash(b"<html>todo</html>", "index.html") in body


def test_content_hash_matches_wrangler():
    """Key format is byte-exact with wrangler's hashFile: blake3(b64+ext)[:32]."""
    h = deploy._content_hash(b"<html>hi</html>", "index.html")
    assert len(h) == 32
    assert h.isalnum()
    # different extension -> different key even for same content
    assert deploy._content_hash(b"<html>hi</html>", "index.js") != h


def test_deploy_pages_end_to_end(fake_cf, tmp_path, monkeypatch):
    backend, _ = fake_cf
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    # sandbox stubs
    monkeypatch.setattr(sandbox, "exec", lambda slug, cmd, timeout=None: {
        "ok": True, "stdout": "dist/index.html\ndist/assets/app.js\n", "stderr": "", "exitCode": 0,
    })
    monkeypatch.setattr(
        sandbox, "read_file",
        lambda slug, path: b"<html>todo</html>" if path.endswith("index.html") else b"js()",
    )
    conn = connect()
    # deploy reads the CF token from env (vault is digest-only by design)
    from factory import apps
    from factory.project import create

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    proj = create(conn, slug="smoke", name="Smoke")
    apps._write_meta(conn, proj["id"], "apps/cloudflare.json",
                     {"installed": True, "account_id": "acct-1", "account_name": "Acct"})
    out = deploy.deploy_pages(conn, proj["id"], "smoke", account_id="acct-1", project="factory-todo")
    assert out["url"] == "https://factory-todo.pages.dev"
    assert out["files"] == 2
    conn.close()
