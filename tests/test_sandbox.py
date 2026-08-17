"""Sandbox gateway client tests: fake gateway over http.server, no network."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from factory import sandbox


def make_handler(seen):
    """A gateway that records every request and answers canned JSON."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            payload = json.loads(body) if body else None
            seen.append((self.command, self.path, dict(self.headers), payload))

        def _reply(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self):
            self._record()
            path = self.path.split("?")[0]
            payload = seen[-1][3]
            if path == "/ensure":
                ok = payload.get("project") != "bad"
                self._reply({"ok": ok, "sandbox_id": "sbx-1"})
            elif path == "/exec":
                self._reply(
                    {"ok": True, "stdout": "hello\n", "stderr": "", "exitCode": 0}
                )
            elif path.startswith("/files/"):
                if self.command == "PUT":
                    self._reply({"ok": True})
                elif "RAW" in self.path:
                    self._reply({"ok": True, "content": "plain-text", "encoding": "utf-8"})
                else:
                    content = base64.b64encode(b"file-bytes").decode("ascii")
                    self._reply({"ok": True, "content": content, "encoding": "base64"})
            elif path == "/ports/expose":
                self._reply({"ok": True, "url": "https://p1.sbx.dev/preview"})
            elif path == "/sync":
                self._reply({"ok": True, "workspace": "/workspace/p1"})
            elif path == "/sleep":
                self._reply({"ok": True})
            elif path == "/destroy":
                self._reply({"ok": True})
            else:
                self._reply({"ok": False, "error": "not found"}, status=404)

        do_GET = _route
        do_POST = _route
        do_PUT = _route

    return Handler


@pytest.fixture
def gw(monkeypatch):
    seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(seen))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SANDBOX_GW_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("SANDBOX_GW_TOKEN", "test-token")
    yield seen
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_ensure(gw):
    out = sandbox.ensure("p1")
    assert out == {"ok": True, "sandbox_id": "sbx-1"}
    method, path, _, payload = gw[-1]
    assert (method, path) == ("POST", "/ensure")
    assert payload == {"project": "p1"}


def test_exec(gw):
    out = sandbox.exec("p1", "ls -la", timeout=5)
    assert out["stdout"] == "hello\n"
    assert out["exitCode"] == 0
    _, _, _, payload = gw[-1]
    assert payload == {"project": "p1", "command": "ls -la", "timeout": 5}


def test_exec_without_timeout_omits_key(gw):
    sandbox.exec("p1", "ls")
    _, _, _, payload = gw[-1]
    assert payload == {"project": "p1", "command": "ls"}


def test_write_file_sends_base64(gw):
    data = b"\x00\x01binary\xff"
    out = sandbox.write_file("p1", "src/main.tsx", data)
    assert out == {"ok": True}
    method, path, _, payload = gw[-1]
    assert (method, path) == ("PUT", "/files/p1/src/main.tsx")
    assert payload == {
        "content": base64.b64encode(data).decode("ascii"),
        "encoding": "base64",
    }


def test_read_file_decodes_base64(gw):
    assert sandbox.read_file("p1", "src/main.tsx") == b"file-bytes"
    method, path, _, _ = gw[-1]
    assert (method, path) == ("GET", "/files/p1/src/main.tsx")


def test_read_file_accepts_raw_text(gw):
    # A gateway may answer with plain text content; the client falls back.
    out = sandbox.read_file("p1", "RAW.txt")
    assert out == b"plain-text"


def test_expose_port_returns_url(gw):
    # local gateway: SDK maps `localhost` to *.localhost preview URLs, then
    # the non-default gateway dev port is appended for local browsing.
    url = sandbox.expose_port("p1", 5173)
    assert url.startswith("https://p1.sbx.dev:") and url.endswith("/preview")
    _, _, _, payload = gw[-1]
    assert payload == {"project": "p1", "port": 5173, "hostname": "localhost"}


def test_expose_port_explicit_hostname(gw):
    url = sandbox.expose_port("p1", 5173, hostname="preview.example.com")
    assert url == "https://p1.sbx.dev:{gwport}/preview" or url.startswith("https://p1.sbx.dev:")
    _, _, _, payload = gw[-1]
    assert payload == {"project": "p1", "port": 5173, "hostname": "preview.example.com"}


def test_sync(gw, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    from factory.db import connect
    from factory import files

    conn = connect()
    files.put(conn, "prj_1", "hello.txt", "hi there")
    files.put(conn, "prj_1", "sub/nested.md", "# n")
    out = sandbox.sync(conn, "prj_1", "p1")
    assert out["workspace"] == "/workspace/p1"
    assert out["files"] == 2
    puts = [p for p in gw if p[0] == "PUT" and p[1].startswith("/files/")]
    assert len(puts) == 2
    paths = {p[1] for p in puts}
    assert "/files/p1/hello.txt" in paths and "/files/p1/sub/nested.md" in paths
    _, _, _, payload = gw[-1]
    assert payload == {"project": "p1"}
    conn.close()


def test_destroy(gw):
    assert sandbox.destroy("p1") == {"ok": True}
    _, _, _, payload = gw[-1]
    assert payload == {"project": "p1"}


def test_auth_header_sent(gw):
    sandbox.ensure("p1")
    _, _, headers, _ = gw[-1]
    assert headers.get("Authorization") == "Bearer test-token"


def test_not_ok_raises(gw):
    with pytest.raises(sandbox.SandboxError):
        sandbox.ensure("bad")


def test_connection_error_raises(gw, monkeypatch):
    monkeypatch.setenv("SANDBOX_GW_URL", "http://127.0.0.1:1")
    with pytest.raises(sandbox.SandboxError):
        sandbox.ensure("p1")
