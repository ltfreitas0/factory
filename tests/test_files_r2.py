"""R2-backed files backend tests: fake in-process S3 server, real SigV4 signing.

The fake server recomputes the AWS Signature V4 signature from the raw
request (canonical request + string to sign, service "s3", region "auto")
and rejects requests with a bad signature, so the client's signing code is
exercised end-to-end. All storage is an in-memory dict.
"""

import hashlib
import hmac
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from factory import files

ACCESS_KEY = "test-access-key"
SECRET_KEY = "test-secret-key"
BUCKET = "test-bucket"
REGION = "auto"
SERVICE = "s3"
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _signing_key(date_stamp: str) -> bytes:
    def hm(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    return hm(
        hm(hm(hm(("AWS4" + SECRET_KEY).encode(), date_stamp), REGION), SERVICE),
        "aws4_request",
    )


def _sigv4_valid(handler: BaseHTTPRequestHandler) -> bool:
    """Recompute the expected signature from the received request."""
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("AWS4-HMAC-SHA256 "):
        return False
    fields = dict(
        p.strip().split("=", 1) for p in auth[len("AWS4-HMAC-SHA256 "):].split(",")
    )
    amz_date = handler.headers.get("x-amz-date", "")
    date_stamp = amz_date[:8]
    signed_headers = fields.get("SignedHeaders", "")
    parsed = urllib.parse.urlsplit(handler.path)
    canonical_headers = "".join(
        f"{name}:{handler.headers.get(name, '').strip()}\n"
        for name in signed_headers.split(";")
    )
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    )
    canonical_request = "\n".join(
        [
            handler.command,
            parsed.path or "/",
            canonical_query,
            canonical_headers,
            signed_headers,
            handler.headers.get("x-amz-content-sha256", ""),
        ]
    )
    scope = f"{date_stamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    expected = hmac.new(
        _signing_key(date_stamp), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, fields.get("Signature", ""))


def _list_xml(store: dict, prefix: str, delimiter: str | None) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<ListBucketResult xmlns="{S3_NS}">',
        f"<Name>{BUCKET}</Name>",
        f"<Prefix>{prefix}</Prefix>",
        "<IsTruncated>false</IsTruncated>",
    ]
    seen: set[str] = set()
    for key in sorted(k for k in store if k.startswith(prefix)):
        if delimiter:
            rest = key[len(prefix):]
            if delimiter in rest:
                common = prefix + rest.split(delimiter, 1)[0] + delimiter
                if common not in seen:
                    seen.add(common)
                    lines.append(f"<CommonPrefixes><Prefix>{common}</Prefix></CommonPrefixes>")
                continue
        lines.append(
            f"<Contents><Key>{key}</Key><Size>{len(store[key])}</Size>"
            "<LastModified>2026-08-16T00:00:00.000Z</LastModified></Contents>"
        )
    lines.append("</ListBucketResult>")
    return "\n".join(lines).encode()


class FakeR2(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence request logging
        pass

    def _parts(self):
        parsed = urllib.parse.urlsplit(self.path)
        bits = parsed.path.strip("/").split("/", 1)
        bucket = bits[0] if bits else ""
        key = urllib.parse.unquote(bits[1]) if len(bits) > 1 else ""
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        return bucket, key, query

    def _send(self, status: int, body: bytes = b"", ctype: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        if ctype:
            self.send_header("Content-Type", ctype)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if not _sigv4_valid(self):
            return self._send(403)
        _, key, query = self._parts()
        if "list-type" in query:
            prefix = query.get("prefix", [""])[0]
            delimiter = query.get("delimiter", [None])[0]
            return self._send(200, _list_xml(self.server.store, prefix, delimiter), "application/xml")
        if key not in self.server.store:
            return self._send(404)
        self._send(200, self.server.store[key])

    def do_PUT(self):
        if not _sigv4_valid(self):
            return self._send(403)
        _, key, _ = self._parts()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.headers.get("x-amz-content-sha256") != hashlib.sha256(body).hexdigest():
            return self._send(400)
        copy_source = self.headers.get("x-amz-copy-source")
        if copy_source:
            src = copy_source.lstrip("/")
            if src.startswith(BUCKET + "/"):
                src = src[len(BUCKET) + 1:]
            src = urllib.parse.unquote(src)
            if src not in self.server.store:
                return self._send(404)
            self.server.store[key] = self.server.store[src]
            xml = f'<CopyObjectResult xmlns="{S3_NS}"><ETag>"x"</ETag></CopyObjectResult>'
            self._send(200, xml.encode(), "application/xml")
        else:
            self.server.store[key] = body
            self._send(200)

    def do_DELETE(self):
        if not _sigv4_valid(self):
            return self._send(403)
        _, key, _ = self._parts()
        self.server.store.pop(key, None)
        self._send(204)


@pytest.fixture
def r2_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeR2)
    server.store = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "f.db")
    conn.row_factory = sqlite3.Row
    files.ensure_schema(conn)
    return conn


@pytest.fixture
def r2(r2_server, monkeypatch):
    monkeypatch.setenv("FACTORY_FILES_BACKEND", "r2")
    monkeypatch.setenv("R2_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.setenv("R2_BUCKET", BUCKET)
    monkeypatch.setenv("R2_ENDPOINT", f"http://127.0.0.1:{r2_server.server_port}")
    return r2_server


# --------------------------------------------------------------------------
# R2 mode
# --------------------------------------------------------------------------


def test_r2_put_get_roundtrip(db, r2):
    out = files.put(db, "p1", "context/spec.md", "# hi")
    assert out["set"] is True
    assert r2.store["repo/p1/context/spec.md"] == b"# hi"
    got = files.get(db, "p1", "context/spec.md")
    assert got["body"] == "# hi"
    assert got["set"] is True


def test_r2_put_bytes_binary_key_and_body(db, r2):
    data = b"\x00\x01\x02binary"
    files.put_bytes(db, "p1", "assets/blob.bin", data)
    assert r2.store["repo/p1/assets/blob.bin"] == data
    # binary bodies stay opaque, as in sqlite mode
    assert files.get(db, "p1", "assets/blob.bin")["body"] == ""


def test_r2_delete_removes_object_and_row(db, r2):
    files.put(db, "p1", "context/a.md", "1")
    files.delete(db, "p1", "context/a.md")
    assert "repo/p1/context/a.md" not in r2.store
    with pytest.raises(files.FileError):
        files.get(db, "p1", "context/a.md")


def test_r2_vault_never_returns_bytes(db, r2):
    files.put(db, "p1", "INGEST_TOKEN", "secret-plain", store="vault")
    got = files.get(db, "p1", "INGEST_TOKEN", store="vault")
    assert got["set"] is True
    assert "body" not in got or got.get("body") in (None, "")
    assert "secret-plain" not in str(got)
    assert all(b"secret-plain" not in v for v in r2.store.values())
    assert files.vault_matches(db, "p1", "INGEST_TOKEN", "secret-plain")
    assert not files.vault_matches(db, "p1", "INGEST_TOKEN", "nope")


def test_r2_list_prefix(db, r2):
    files.put(db, "p1", "context/a.md", "1")
    files.put(db, "p1", "context/b.md", "2")
    files.put(db, "p1", "agents/build.md", "3")
    names = [r["path"] for r in files.list_prefix(db, "p1", "context/")]
    assert names == ["context/a.md", "context/b.md"]


def test_r2_list_prefix_falls_back_to_sqlite(db, r2, monkeypatch):
    files.put(db, "p1", "context/a.md", "1")
    files.put(db, "p1", "agents/build.md", "3")
    monkeypatch.setenv("R2_ENDPOINT", "http://127.0.0.1:1")  # unreachable
    names = [r["path"] for r in files.list_prefix(db, "p1", "")]
    assert names == ["agents/build.md", "context/a.md"]


def test_r2_snapshot_roundtrip(db, r2):
    files.put(db, "p1", "context/a.md", "1")
    files.put(db, "p1", "context/b.md", "2")
    n = files.snapshot_state(db, "p1", "abc123")
    assert n == 2
    assert r2.store["repo/p1/snapshots/abc123/context/a.md"] == b"1"
    assert files.list_snapshots(db, "p1") == ["abc123"]
    # live listing excludes snapshot copies
    assert [r["path"] for r in files.list_prefix(db, "p1", "")] == [
        "context/a.md",
        "context/b.md",
    ]
    # mutate state, then restore brings it back
    files.delete(db, "p1", "context/a.md")
    assert files.get(db, "p1", "context/b.md")["body"] == "2"
    files.restore_snapshot(db, "p1", "abc123")
    assert files.get(db, "p1", "context/a.md")["body"] == "1"
    assert files.get(db, "p1", "context/b.md")["body"] == "2"


def test_r2_snapshot_state_overwrites(db, r2):
    files.put(db, "p1", "a.md", "1")
    files.snapshot_state(db, "p1", "s1")
    files.put(db, "p1", "a.md", "v2")
    assert files.snapshot_state(db, "p1", "s1") == 1
    assert r2.store["repo/p1/snapshots/s1/a.md"] == b"v2"


def test_r2_snapshots_do_not_recursively_snapshot(db, r2):
    files.put(db, "p1", "a.md", "1")
    files.snapshot_state(db, "p1", "one")
    files.put(db, "p1", "b.md", "2")
    assert files.snapshot_state(db, "p1", "two") == 2
    assert "repo/p1/snapshots/two/snapshots/one/a.md" not in r2.store
    assert files.list_snapshots(db, "p1") == ["one", "two"]


def test_r2_restore_missing_snapshot(db, r2):
    with pytest.raises(files.FileError, match="snapshot not found"):
        files.restore_snapshot(db, "p1", "nope")


def test_r2_path_traversal_rejected(db, r2):
    with pytest.raises(files.FileError):
        files.put(db, "p1", "../etc/passwd", "x")
    with pytest.raises(files.FileError):
        files.get(db, "p1", "/abs")


def test_r2_missing_config_raises(db, monkeypatch):
    monkeypatch.setenv("FACTORY_FILES_BACKEND", "r2")
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    with pytest.raises(RuntimeError):
        files.put(db, "p1", "a.md", "x")


# --------------------------------------------------------------------------
# sqlite mode: snapshot helpers on the disk root
# --------------------------------------------------------------------------


def _with_repo_path(conn, project_id: str, repo_path) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS projects
           (id TEXT PRIMARY KEY, slug TEXT, repo_path TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO projects (id, slug, repo_path) VALUES (?, ?, ?)",
        (project_id, project_id, str(repo_path)),
    )
    conn.commit()


def test_sqlite_mode_snapshot_uses_disk_root(db, tmp_path):
    root = tmp_path / "repo"
    _with_repo_path(db, "p1", root)
    files.put_bytes(db, "p1", "a.md", b"one")
    assert (root / "a.md").read_text() == "one"
    assert files.snapshot_state(db, "p1", "sha1") == 1
    assert (root / "snapshots" / "sha1" / "a.md").read_text() == "one"
    assert files.list_snapshots(db, "p1") == ["sha1"]
    files.delete(db, "p1", "a.md")
    files.restore_snapshot(db, "p1", "sha1")
    assert (root / "a.md").read_text() == "one"


def test_sqlite_mode_snapshot_requires_disk_root(db):
    with pytest.raises(files.FileError, match="r2-only"):
        files.snapshot_state(db, "p1", "sha1")
    with pytest.raises(files.FileError, match="r2-only"):
        files.restore_snapshot(db, "p1", "sha1")
    with pytest.raises(files.FileError, match="r2-only"):
        files.list_snapshots(db, "p1")


def test_sqlite_mode_default_backend_untouched(db):
    # sanity: default backend still behaves exactly as before
    files.put(db, "p1", "x.md", "hello")
    assert files.get(db, "p1", "x.md")["body"] == "hello"
    files.put(db, "p1", "SECRET", "abc", store="vault")
    assert files.vault_matches(db, "p1", "SECRET", "abc")
