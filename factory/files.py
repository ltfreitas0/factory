"""Project files: one API, two roots (repo | vault).

Vault values are never returned after write. Paths are relative; `..` is
rejected. Isolated from tickets and the board.

Two storage backends, selected by `FACTORY_FILES_BACKEND`:

- unset / "sqlite" (default): bodies live in the `files` sqlite table,
  mirrored to the project disk root when one exists (today's behavior).
- "r2": objects live in Cloudflare R2 (S3-compatible, SigV4-signed via
  stdlib urllib; requires R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
  R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT). The sqlite rows stay as
  the metadata index (path/updated_at; body column holds "" for repo and
  the HMAC digest for vault). Key layout:
    repo/{project_id}/{path}                    mutable state
    repo/{project_id}/snapshots/{sha}/{path}    immutable snapshots
    vault/{project_id}/{path}                   digest only, GET never returns bytes
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SAFE = re.compile(r"^[A-Za-z0-9._/-]+$")

_R2_REGION = "auto"
_R2_SERVICE = "s3"
_R2_REQUIRED_ENV = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_ENDPOINT",
)


class FileError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _commit(conn) -> None:
    try:
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "no transaction is active" not in str(exc):
            raise


def normalize(path: str) -> str:
    p = (path or "").strip().lstrip("/")
    if not p or p in {".", ".."}:
        raise FileError("invalid path")
    parts = p.split("/")
    if any(part in {"", ".", ".."} for part in parts) or not SAFE.match(p):
        raise FileError(f"illegal path: {path!r}")
    return p


def ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS files (
             project_id TEXT NOT NULL,
             store TEXT NOT NULL,
             path TEXT NOT NULL,
             body TEXT,
             updated_at TEXT NOT NULL,
             PRIMARY KEY (project_id, store, path)
           )"""
    )
    _commit(conn)


def disk_root(conn, project_id: str) -> Path | None:
    try:
        row = conn.execute(
            "SELECT repo_path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    raw = row["repo_path"] if isinstance(row, sqlite3.Row) else row[0]
    if not raw:
        return None
    return Path(raw)


# --------------------------------------------------------------------------
# R2 backend (FACTORY_FILES_BACKEND=r2)
# --------------------------------------------------------------------------


def _r2_enabled() -> bool:
    return os.environ.get("FACTORY_FILES_BACKEND", "").strip().lower() == "r2"


def _r2_config() -> dict[str, str]:
    missing = [name for name in _R2_REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "FACTORY_FILES_BACKEND=r2 requires env: " + ", ".join(missing)
        )
    return {name: os.environ[name] for name in _R2_REQUIRED_ENV}


def _aws_quote(value: str, safe: str = "/") -> str:
    return urllib.parse.quote(value, safe=safe)


def _sigv4_headers(
    method: str,
    url: str,
    payload: bytes,
    headers: dict[str, str],
    cfg: dict[str, str],
) -> dict[str, str]:
    """Sign a request with AWS Signature V4 (service s3, region auto).

    Canonical request:
        METHOD \\n canonical-uri \\n canonical-query \\n canonical-headers \\n
        signed-headers \\n sha256-hex(payload)
    String to sign:
        AWS4-HMAC-SHA256 \\n amz-date \\n {date}/auto/s3/aws4_request \\n
        sha256-hex(canonical-request)
    """
    parsed = urllib.parse.urlsplit(url)
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    signed = dict(headers)
    signed["host"] = parsed.netloc
    signed["x-amz-content-sha256"] = payload_hash
    signed["x-amz-date"] = amz_date

    canonical_headers = "".join(
        f"{name.lower()}:{str(signed[name]).strip()}\n" for name in sorted(signed)
    )
    signed_headers = ";".join(sorted(name.lower() for name in signed))

    canonical_query = "&".join(
        f"{_aws_quote(k, safe='-_.~')}={_aws_quote(v, safe='-_.~')}"
        for k, v in sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    )
    canonical_request = "\n".join(
        [
            method.upper(),
            parsed.path or "/",
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    scope = f"{date_stamp}/{_R2_REGION}/{_R2_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_signing = _hmac(
        _hmac(
            _hmac(
                _hmac(("AWS4" + cfg["R2_SECRET_ACCESS_KEY"]).encode("utf-8"), date_stamp),
                _R2_REGION,
            ),
            _R2_SERVICE,
        ),
        "aws4_request",
    )
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    signed["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={cfg['R2_ACCESS_KEY_ID']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return signed


def _r2_request(
    method: str,
    key: str,
    *,
    body: bytes | None = None,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """One signed R2 request. Returns (status, response-body).

    HTTP error statuses are returned as-is; network failures raise FileError.
    """
    cfg = _r2_config()
    url = f"{cfg['R2_ENDPOINT'].rstrip('/')}/{_aws_quote(cfg['R2_BUCKET'])}"
    if key:
        url += f"/{_aws_quote(key)}"
    if query:
        url += "?" + "&".join(
            f"{_aws_quote(k, safe='-_.~')}={_aws_quote(v, safe='-_.~')}"
            for k, v in sorted(query.items())
        )
    payload = body if body is not None else b""
    signed = _sigv4_headers(method, url, payload, headers or {}, cfg)
    request = urllib.request.Request(
        url,
        data=payload if method in {"PUT", "POST"} else None,
        method=method,
        headers=signed,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:1024]
    except (urllib.error.URLError, OSError) as exc:
        raise FileError(f"r2 {method} {key or ''} unreachable: {exc}") from exc


def _r2_put(key: str, body: bytes) -> None:
    status, raw = _r2_request("PUT", key, body=body)
    if not 200 <= status < 300:
        raise FileError(f"r2 put {key} failed: HTTP {status}: {raw!r}")


def _r2_get(key: str) -> bytes | None:
    status, raw = _r2_request("GET", key)
    if status == 404:
        return None
    if not 200 <= status < 300:
        raise FileError(f"r2 get {key} failed: HTTP {status}: {raw!r}")
    return raw


def _r2_delete(key: str) -> None:
    status, raw = _r2_request("DELETE", key)
    if status == 404:
        return
    if not 200 <= status < 300:
        raise FileError(f"r2 delete {key} failed: HTTP {status}: {raw!r}")


def _r2_copy(source_key: str, dest_key: str) -> None:
    """Server-side copy (S3 CopyObject) via PUT + x-amz-copy-source."""
    cfg = _r2_config()
    copy_source = f"/{_aws_quote(cfg['R2_BUCKET'])}/{_aws_quote(source_key)}"
    status, raw = _r2_request(
        "PUT", dest_key, body=b"", headers={"x-amz-copy-source": copy_source}
    )
    if not 200 <= status < 300:
        raise FileError(
            f"r2 copy {source_key} -> {dest_key} failed: HTTP {status}: {raw!r}"
        )


_S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _r2_list(prefix: str, delimiter: str | None = None) -> list[str]:
    """List R2 object keys under a prefix (ListObjectsV2)."""
    query = {"list-type": "2", "prefix": prefix}
    if delimiter:
        query["delimiter"] = delimiter
    status, raw = _r2_request("GET", "", query=query)
    if not 200 <= status < 300:
        raise FileError(f"r2 list {prefix!r} failed: HTTP {status}: {raw!r}")
    root = ET.fromstring(raw)
    keys: list[str] = []
    for contents in root.iter(f"{{{_S3_NS}}}Contents"):
        key_el = contents.find(f"{{{_S3_NS}}}Key")
        if key_el is not None and key_el.text:
            keys.append(key_el.text)
    if delimiter:
        for common in root.iter(f"{{{_S3_NS}}}CommonPrefixes"):
            pfx = common.find(f"{{{_S3_NS}}}Prefix")
            if pfx is not None and pfx.text:
                keys.append(pfx.text)
    return keys


def _r2_key(project_id: str, path: str, store: str = "repo") -> str:
    root = "vault" if store == "vault" else "repo"
    return f"{root}/{project_id}/{path}"


def _state_prefix(project_id: str) -> str:
    return _r2_key(project_id, "")


def _snap_prefix(project_id: str, sha: str) -> str:
    return f"{_state_prefix(project_id)}snapshots/{sha}/"


def _upsert_row(conn, project_id: str, store: str, path: str, body: str) -> None:
    ensure_schema(conn)
    conn.execute(
        """INSERT INTO files (project_id, store, path, body, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(project_id, store, path) DO UPDATE SET
             body = excluded.body, updated_at = excluded.updated_at""",
        (project_id, store, path, body, _now()),
    )
    _commit(conn)


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------


def put(conn, project_id: str, path: str, body: str, store: str = "repo") -> dict:
    if store not in {"repo", "vault"}:
        raise FileError(f"bad store: {store}")
    path = normalize(path)
    if store == "vault":
        # store a hash for compare; raw only lives in this write if caller
        # keeps it. We persist HMAC-sha256 of the value with a static local
        # pepper so GET never has material to leak.
        body = _digest(body)
    if _r2_enabled():
        _r2_put(_r2_key(project_id, path, store), body.encode())
        # sqlite row is the metadata index: repo keeps an empty body, vault
        # keeps the digest, as in sqlite mode.
        _upsert_row(conn, project_id, store, path, body if store == "vault" else "")
        return describe(store, path, set_=True, bytes_=len((body or "").encode()), kind="text")
    _upsert_row(conn, project_id, store, path, body)
    return describe(store, path, set_=True, bytes_=len((body or "").encode()), kind="text")


def put_bytes(
    conn,
    project_id: str,
    path: str,
    data: bytes,
    store: str = "repo",
    *,
    mime: str = "",
    sync_disk: bool = True,
) -> dict:
    """Upload a file. Text is stored in the index; binary bodies stay opaque
    (disk mirror or R2) with an empty sqlite row."""
    if store not in {"repo", "vault"}:
        raise FileError(f"bad store: {store}")
    path = normalize(path)
    data = data or b""
    text: str | None
    try:
        text = data.decode("utf-8")
        if "\x00" in text:
            text = None
    except UnicodeDecodeError:
        text = None
    if store == "vault":
        if text is None:
            raise FileError("vault values must be text")
        return put(conn, project_id, path, text, store="vault")
    if _r2_enabled():
        _r2_put(_r2_key(project_id, path, store), data)
        _upsert_row(conn, project_id, store, path, "")
        kind = "text" if text is not None else "binary"
        return describe(store, path, set_=True, bytes_=len(data), kind=kind, mime=mime)
    if sync_disk:
        root = disk_root(conn, project_id)
        if root:
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    body = text if text is not None else ""
    _upsert_row(conn, project_id, store, path, body)
    kind = "text" if text is not None else "binary"
    return describe(store, path, set_=True, bytes_=len(data), kind=kind, mime=mime)


def get(conn, project_id: str, path: str, store: str = "repo") -> dict:
    path = normalize(path)
    ensure_schema(conn)
    row = conn.execute(
        "SELECT body, updated_at FROM files WHERE project_id=? AND store=? AND path=?",
        (project_id, store, path),
    ).fetchone()
    if row is None:
        raise FileError(f"not found: {store}:{path}")
    if store == "vault":
        return describe(store, path, set_=True, updated_at=row["updated_at"])
    if _r2_enabled():
        data = _r2_get(_r2_key(project_id, path, store))
        if data is None:
            raise FileError(f"not found: {store}:{path}")
        try:
            text = data.decode("utf-8")
            body = "" if "\x00" in text else text
        except UnicodeDecodeError:
            body = ""  # binary bodies stay opaque, as in sqlite mode
        return {
            "store": store,
            "path": path,
            "body": body,
            "set": True,
            "updated_at": row["updated_at"],
        }
    return {
        "store": store,
        "path": path,
        "body": row["body"],
        "set": True,
        "updated_at": row["updated_at"],
    }


def delete(conn, project_id: str, path: str, store: str = "repo") -> None:
    path = normalize(path)
    ensure_schema(conn)
    conn.execute(
        "DELETE FROM files WHERE project_id=? AND store=? AND path=?",
        (project_id, store, path),
    )
    _commit(conn)
    if _r2_enabled():
        _r2_delete(_r2_key(project_id, path, store))
        return
    if store == "repo":
        root = disk_root(conn, project_id)
        if root:
            dest = root / path
            if dest.is_file():
                dest.unlink()


def list_prefix(conn, project_id: str, prefix: str = "", store: str = "repo") -> list[dict]:
    prefix = prefix.strip().lstrip("/")
    if prefix:
        normalize(prefix.rstrip("/"))
    ensure_schema(conn)
    if _r2_enabled() and store == "repo":
        try:
            state_p = _state_prefix(project_id)
            out = []
            for key in sorted(_r2_list(state_p + prefix)):
                if not key.startswith(state_p):
                    continue
                rel = key[len(state_p):]
                if rel.startswith("snapshots/") and not prefix.startswith("snapshots/"):
                    continue  # snapshot copies are not part of the live state
                out.append(describe(store, rel, set_=True))
            return out
        except RuntimeError:
            raise  # missing R2 config is a server error, never a silent fallback
        except Exception:
            pass  # R2 listing unavailable -> fall back to the sqlite index
    rows = conn.execute(
        """SELECT path, updated_at FROM files
           WHERE project_id=? AND store=? AND path LIKE ?
           ORDER BY path""",
        (project_id, store, f"{prefix}%" if prefix else "%"),
    ).fetchall()
    return [describe(store, r["path"], set_=True, updated_at=r["updated_at"]) for r in rows]


def vault_matches(conn, project_id: str, path: str, plaintext: str) -> bool:
    """Constant-time compare of a presented secret to the stored digest."""
    path = normalize(path)
    ensure_schema(conn)
    row = conn.execute(
        "SELECT body FROM files WHERE project_id=? AND store='vault' AND path=?",
        (project_id, path),
    ).fetchone()
    if row is None or not row["body"]:
        return False
    return hmac.compare_digest(row["body"], _digest(plaintext))


def snapshot_state(conn, project_id: str, sha: str) -> int:
    """Copy all live repo objects to `repo/{pid}/snapshots/{sha}/`.

    Server-side copy in R2 mode; copies the project disk tree in sqlite mode.
    Returns the number of objects copied.
    """
    sha = (sha or "").strip()
    if not sha or not re.fullmatch(r"[A-Za-z0-9._-]+", sha):
        raise FileError("invalid snapshot sha")
    if _r2_enabled():
        state_p = _state_prefix(project_id)
        snap_p = _snap_prefix(project_id, sha)
        keys = [
            key
            for key in _r2_list(state_p)
            if not key.startswith(f"{state_p}snapshots/")
        ]
        for key in keys:
            _r2_copy(key, snap_p + key[len(state_p):])
        return len(keys)
    root = disk_root(conn, project_id)
    if not root:
        raise FileError("r2-only")
    snap = root / "snapshots" / sha
    if snap.exists():
        shutil.rmtree(snap)
    snap.mkdir(parents=True)
    snap_root = root / "snapshots"
    count = 0
    for src in sorted(p for p in root.rglob("*") if p.is_file()):
        if src.is_relative_to(snap_root):
            continue  # never snapshot previous snapshots
        dest = snap / src.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        count += 1
    return count


def restore_snapshot(conn, project_id: str, sha: str) -> None:
    """Copy `repo/{pid}/snapshots/{sha}/*` back over the live state."""
    sha = (sha or "").strip()
    if not sha or not re.fullmatch(r"[A-Za-z0-9._-]+", sha):
        raise FileError("invalid snapshot sha")
    if _r2_enabled():
        state_p = _state_prefix(project_id)
        snap_p = _snap_prefix(project_id, sha)
        keys = _r2_list(snap_p)
        if not keys:
            raise FileError(f"snapshot not found: {sha}")
        for key in keys:
            rel = key[len(snap_p):]
            _r2_copy(key, state_p + rel)
            _upsert_row(conn, project_id, "repo", rel, "")
        return
    root = disk_root(conn, project_id)
    if not root:
        raise FileError("r2-only")
    src = root / "snapshots" / sha
    if not src.is_dir():
        raise FileError(f"snapshot not found: {sha}")
    for f in src.rglob("*"):
        if f.is_file():
            dest = root / f.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)


def list_snapshots(conn, project_id: str) -> list[str]:
    """Distinct snapshot shas for a project, oldest first."""
    if _r2_enabled():
        snap_root = f"{_state_prefix(project_id)}snapshots/"
        shas: list[str] = []
        for key in _r2_list(snap_root, delimiter="/"):
            rel = key[len(snap_root):].rstrip("/")
            if rel and rel not in shas:
                shas.append(rel)
        return sorted(shas)
    root = disk_root(conn, project_id)
    if not root:
        raise FileError("r2-only")
    snap_root = root / "snapshots"
    if not snap_root.is_dir():
        return []
    return sorted(d.name for d in snap_root.iterdir() if d.is_dir())


def describe(
    store: str,
    path: str,
    set_: bool,
    updated_at: str | None = None,
    *,
    bytes_: int | None = None,
    kind: str | None = None,
    mime: str | None = None,
) -> dict:
    out = {"store": store, "path": path, "set": set_, "updated_at": updated_at}
    if bytes_ is not None:
        out["bytes"] = bytes_
    if kind:
        out["kind"] = kind
    if mime:
        out["mime"] = mime
    return out


def _digest(value: str) -> str:
    # Local-only pepper; sandbox. Real encryption can replace this later
    # without changing the files surface.
    return hmac.new(b"factory-vault-v1", (value or "").encode(), hashlib.sha256).hexdigest()
