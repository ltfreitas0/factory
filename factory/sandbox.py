"""Sandbox gateway client: remote exec + files for per-project sandboxes.

Talks to the sandbox-gw Worker (see sandbox-gw/) over plain HTTP. Every
request carries the gateway token as ``Authorization: Bearer <token>`` and
bodies are JSON. Transport is urllib so this module runs with only stdlib
(no requests/httpx at runtime — httpx is a dev-only dependency).

Configuration (read from env on every call, so tests can monkeypatch):
    SANDBOX_GW_URL   base URL, default http://127.0.0.1:8787
    SANDBOX_GW_TOKEN bearer token for the gateway
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_GW_URL = "http://127.0.0.1:8787"
TIMEOUT = 20  # seconds; a sandbox exec can legitimately take a while


class SandboxError(ValueError):
    """The gateway returned an error or was unreachable."""


def _base_url() -> str:
    return os.environ.get("SANDBOX_GW_URL", DEFAULT_GW_URL).rstrip("/")


def _token() -> str:
    return os.environ.get("SANDBOX_GW_TOKEN", "")


def _quote(path: str) -> str:
    """Quote a sandbox file path for use inside the URL, keeping slashes."""
    return urllib.parse.quote(path.strip().lstrip("/"), safe="/")


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one JSON request to the gateway; return the parsed response dict."""
    url = _base_url() + path
    data = None
    headers = {"Authorization": f"Bearer {_token()}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        snippet = exc.read(200)
        raise SandboxError(f"{method} {path}: HTTP {exc.code}: {snippet!r}") from exc
    except urllib.error.URLError as exc:
        raise SandboxError(f"{method} {path}: {exc.reason}") from exc
    if not body:
        raise SandboxError(f"{method} {path}: empty response")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SandboxError(f"{method} {path}: non-JSON response: {body[:200]!r}") from exc


def _check(data: dict[str, Any], path: str) -> dict[str, Any]:
    """Raise SandboxError unless the gateway answered ``ok``."""
    if not data.get("ok"):
        raise SandboxError(f"{path}: gateway refused: {data}")
    return data


def ensure(project: str) -> dict[str, Any]:
    """Provision (or reuse) the project's sandbox; returns {ok, sandbox_id}."""
    return _check(_request("POST", "/ensure", {"project": project}), "POST /ensure")


def exec(project: str, command: str, timeout: int | None = None) -> dict[str, Any]:
    """Run a command in the sandbox; returns {ok, stdout, stderr, exitCode}."""
    payload: dict[str, Any] = {"project": project, "command": command}
    if timeout is not None:
        payload["timeout"] = timeout
    return _check(_request("POST", "/exec", payload), "POST /exec")


def write_file(project: str, path: str, data: bytes) -> dict[str, Any]:
    """Write bytes into the sandbox workspace.

    Content travels base64 with ``encoding: "base64"`` so the gateway never
    has to guess whether a body is text or binary.
    """
    payload = {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}
    return _check(
        _request("PUT", f"/files/{_quote(project)}/{_quote(path)}", payload),
        f"PUT /files/{path}",
    )


def read_file(project: str, path: str) -> bytes:
    """Read a file from the sandbox workspace as bytes.

    The gateway answers with ``{ok, content, encoding}`` (``encoding`` is
    ``"base64"`` or ``"utf-8"``); decode accordingly. If the encoding field
    is absent, fall back to sniffing: valid base64 wins, else raw UTF-8.
    """
    resp = _check(_request("GET", f"/files/{_quote(project)}/{_quote(path)}"), f"GET /files/{path}")
    content = resp.get("content")
    if not isinstance(content, str):
        raise SandboxError(f"GET /files/{path}: no string content in response")
    if resp.get("encoding") == "base64":
        return base64.b64decode(content)
    if resp.get("encoding") == "utf-8":
        return content.encode("utf-8")
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return content.encode("utf-8")


def expose_port(project: str, port: int, hostname: str | None = None) -> str:
    """Expose a sandbox port; returns the public preview URL.

    Under wrangler dev the SDK maps the host to *.localhost itself, and the
    browser reaches the gateway on its dev port — so when the returned URL
    has no explicit port and the gateway base carries a non-default port,
    it is appended (`http://5173-smoke-<token>.localhost:8787/`).
    """
    body: dict[str, Any] = {"project": project, "port": port}
    if hostname:
        body["hostname"] = hostname
    elif "127.0.0.1" in _base_url() or "localhost" in _base_url():
        # The SDK maps `localhost` to *.localhost preview URLs; a raw IP
        # produces a broken URL. Local wrangler dev listens on 8787, which
        # the port-append below adds back.
        body["hostname"] = "localhost"
    resp = _check(
        _request("POST", "/ports/expose", body),
        "POST /ports/expose",
    )
    url = resp.get("url")
    if not isinstance(url, str) or not url:
        raise SandboxError("POST /ports/expose: no url in response")
    gw = _base_url()
    m = re.match(r"^https?://([^/]+)", gw)
    if m and ":" in m.group(1):
        gw_host = m.group(1)
        gw_port = gw_host.rsplit(":", 1)[1]
        if gw_port not in {"80", "443"}:
            scheme, rest = url.split("://", 1)
            host, _, path = rest.partition("/")
            if ":" not in host:
                host = f"{host}:{gw_port}"
                url = f"{scheme}://{host}/{path}"
    return url


def sync(conn, project_id: str, project: str) -> dict[str, Any]:
    """Copy repo store state into the sandbox workspace.

    Lists every file in the project's repo store and PUTs it into the sandbox
    under /workspace/{project}/. Returns {ok, workspace, files}.
    """
    from factory import files

    entries = files.list_prefix(conn, project_id, prefix="", store="repo")
    pushed = 0
    for e in entries:
        path = e["path"]
        try:
            doc = files.get(conn, project_id, path, store="repo")
        except files.FileError:
            continue
        body = doc.get("body") or ""
        write_file(project, path, body.encode("utf-8"))
        pushed += 1
    resp = _check(_request("POST", "/sync", {"project": project}), "POST /sync")
    resp["files"] = pushed
    return resp


def sleep(project: str) -> dict[str, Any]:
    """Put the sandbox to sleep; returns {ok}."""
    return _check(_request("POST", "/sleep", {"project": project}), "POST /sleep")


def destroy(project: str) -> dict[str, Any]:
    """Destroy the sandbox; returns {ok}."""
    return _check(_request("POST", "/destroy", {"project": project}), "POST /destroy")
