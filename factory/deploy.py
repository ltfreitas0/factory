"""Production deploy: build in the sandbox, direct-upload to Cloudflare Pages.

No wrangler wrapping — speaks the Pages Direct Upload API directly:

1. `bun run build` inside the project sandbox (dist/ produced in /workspace).
2. Pull the dist tree out of the sandbox via the gateway files API.
3. Get a short-lived upload JWT: GET /pages/projects/{p}/upload-token
4. Upload each file as a content-hash-keyed asset: POST /pages/assets/upload
5. Create the deployment with a manifest {path: hash}: POST .../deployments

Token/account come from the project's installed cloudflare app (vault), the
same surface the UI uses — never from env directly.

Also owns the legacy pipeline runner (`run_pipeline`, formerly dispatch.py):
both are "get code to production" and co-change, so they share one module.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from factory import apps, files, obs, sandbox

CF_API = "https://api.cloudflare.com/client/v4"

VAULT_REF = re.compile(r"\$\{vault/([A-Za-z0-9._-]+)\}")


class DeployError(ValueError):
    pass


# --- legacy pipeline.yml runner (ex-dispatch) ----------------------------

def run_pipeline(conn, project_id: str, instance: str = "dev") -> dict:
    """Run the persisted pipeline file. Never invent steps."""
    try:
        pipe = files.get(conn, project_id, "pipeline.yml")
    except files.FileError as exc:
        raise DeployError("no pipeline.yml") from exc
    try:
        inst = files.get(conn, project_id, f"instances/{instance}.json")
        meta = json.loads(inst.get("body") or "{}")
    except (files.FileError, json.JSONDecodeError):
        meta = {"name": instance, "production": instance == "prod"}
    if meta.get("production") and os.environ.get("FACTORY_ALLOW_PROD") != "1":
        # Human/API must set the flag or we refuse — runner never sets it.
        raise DeployError("refusing production dispatch")
    body = _resolve(conn, project_id, pipe.get("body") or "")
    if not body.strip():
        raise DeployError("empty pipeline")
    cwd = Path(os.environ.get("FACTORY_ROOT") or ".")
    obs.emit("dispatch_start", project_id=project_id, instance=instance)
    proc = subprocess.run(
        body,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ},
    )
    ok = proc.returncode == 0
    obs.emit(
        "dispatch_end",
        project_id=project_id,
        instance=instance,
        ok=ok,
    )
    return {
        "ok": ok,
        "instance": instance,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _resolve(conn, project_id: str, text: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        # We only store digests in vault — for dispatch, also allow env fallback.
        env_key = name.replace(".", "_")
        val = os.environ.get(env_key) or os.environ.get(name)
        if not val:
            raise DeployError(f"missing vault/env secret: {name}")
        return val

    return VAULT_REF.sub(repl, text)


def _cf(method: str, url: str, token: str, *, data=None, raw_body: bytes | None = None,
        headers: dict | None = None) -> dict:
    hdrs = {
        "User-Agent": "factory-deploy",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        **(headers or {}),
    }
    body = None
    if raw_body is not None:
        body = raw_body
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise DeployError(f"cf {method} {url}: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DeployError(f"cf unreachable: {exc.reason}") from exc
    if not payload:
        return {}
    return json.loads(payload)


def _cf_json(method: str, url: str, token: str, **kw) -> dict:
    data = _cf(method, url, token, **kw)
    if not data.get("success"):
        errs = data.get("errors") or [{"message": "unknown"}]
        raise DeployError(f"cf error: {errs[0].get('message')}")
    return data.get("result")


def _content_hash(data: bytes, path: str) -> str:
    """Cloudflare's Pages asset key: blake3(base64(file) + ext), 32 hex chars.

    Mirrors wrangler's hashFile (packages/deploy-helpers/src/deploy/helpers/hash.ts).
    """
    import blake3

    base64_contents = __import__("base64").b64encode(data).decode("ascii")
    extension = Path(path).suffix.lstrip(".")
    return blake3.blake3((base64_contents + extension).encode("utf-8")).hexdigest()[:32]


def _mime(path: str) -> str:
    mime = mimetypes.guess_type(path)[0]
    return mime or "application/octet-stream"


def build_in_sandbox(slug: str, build_cmd: str = "bun run build") -> dict:
    """Run the build inside the sandbox; returns {ok, stdout, stderr}."""
    out = sandbox.exec(slug, build_cmd, timeout=600)
    return {"ok": bool(out.get("ok")), "stdout": out.get("stdout", ""), "stderr": out.get("stderr", "")}


def pull_dist(slug: str, dist_dir: str = "dist") -> dict[str, bytes]:
    """Read the built dist tree out of the sandbox. {rel_path: bytes}."""
    listing = sandbox.exec(slug, f"find {dist_dir} -type f | sort", timeout=60)
    if not listing.get("ok"):
        raise DeployError(f"cannot list dist: {listing.get('stderr', '')[:200]}")
    paths = [ln.strip() for ln in (listing.get("stdout") or "").splitlines() if ln.strip()]
    if not paths:
        raise DeployError("build produced no files in dist/")
    out: dict[str, bytes] = {}
    for p in paths:
        rel = p[len(dist_dir):].lstrip("/")
        data = sandbox.read_file(slug, p)
        out[rel] = data
    return out


def _upload_token(account_id: str, project: str, token: str) -> str:
    url = f"{CF_API}/accounts/{account_id}/pages/projects/{project}/upload-token"
    result = _cf_json("GET", url, token)
    jwt = result.get("jwt") if isinstance(result, dict) else None
    if not jwt:
        raise DeployError("upload-token returned no jwt")
    return jwt


def _upload_assets(jwt: str, files_map: dict[str, bytes]) -> None:
    body = []
    for path, data in files_map.items():
        body.append(
            {
                "base64": True,
                "key": _content_hash(data, path),
                "metadata": {"contentType": _mime(path)},
                "value": __import__("base64").b64encode(data).decode("ascii"),
            }
        )
    _cf_json(
        "POST",
        f"{CF_API}/pages/assets/upload",
        jwt,
        data=body,
    )
    # Register the uploaded hashes — without this the deployment references
    # files the asset store doesn't know about and serves 404.
    _cf_json(
        "POST",
        f"{CF_API}/pages/assets/upsert-hashes",
        jwt,
        data={"hashes": [a["key"] for a in body]},
    )


def create_deployment(
    account_id: str,
    project: str,
    token: str,
    files_map: dict[str, bytes],
    *,
    branch: str = "main",
    commit_hash: str = "",
    commit_message: str = "factory deploy",
) -> dict:
    """Upload assets + create the deployment; returns the deployment record."""
    jwt = _upload_token(account_id, project, token)
    _upload_assets(jwt, files_map)
    # wrangler prefixes each manifest key with "/" (see upload.ts return)
    manifest = {f"/{path}": _content_hash(data, path) for path, data in files_map.items()}
    boundary = "----factory" + os.urandom(8).hex()
    parts: list[bytes] = []
    fields = [
        ("branch", branch),
        ("commit_dirty", "false"),
        ("commit_hash", commit_hash),
        ("commit_message", commit_message),
        ("manifest", json.dumps(manifest)),
    ]
    for name, value in fields:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(f"--{boundary}--\r\n".encode())
    raw = b"".join(parts)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    url = f"{CF_API}/accounts/{account_id}/pages/projects/{project}/deployments"
    data = _cf("POST", url, token, raw_body=raw, headers=headers)
    if not data.get("success"):
        errs = data.get("errors") or [{"message": "unknown"}]
        raise DeployError(f"deployment failed: {errs[0].get('message')}")
    return data.get("result") or {}


def deploy_pages(
    conn,
    project_id: str,
    slug: str,
    *,
    account_id: str | None = None,
    project: str | None = None,
    build_cmd: str = "bun run build",
    branch: str = "main",
) -> dict:
    """End-to-end: build in sandbox → direct upload to Pages → deployment record."""
    meta = apps._read_meta(conn, project_id, "apps/cloudflare.json")
    account_id = account_id or meta.get("account_id") or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or ""
    if not account_id:
        raise DeployError("cloudflare account not bound to project")
    token = apps._token(conn, project_id, "cloudflare")
    # NOTE: vault entries are digest-only by design, so OAuth tokens stored
    # there cannot drive API calls; deploys use the env/global token until a
    # separate encrypted credential store lands.
    if not token:
        raise DeployError("cloudflare token not stored for project")
    project = project or meta.get("pages_project") or slug

    obs.emit("deploy_started", project=slug, instance="prod", target="pages",
             pages_project=project, account_id=account_id)
    build = build_in_sandbox(slug, build_cmd)
    if not build["ok"]:
        obs.emit("deploy_fail", project=slug, instance="prod", reason="build",
                 detail=build["stderr"][-500:])
        raise DeployError(f"build failed: {build['stderr'][-300:]}")
    dist = pull_dist(slug)
    dep = create_deployment(
        account_id, project, token, dist,
        branch=branch, commit_message=f"factory deploy {slug}",
    )
    url = dep.get("url") or f"https://{project}.pages.dev"
    obs.emit("deploy_ok", project=slug, instance="prod", target="pages",
             url=url, deployment=dep.get("id"))
    # remember the pages project on the app meta
    if not meta.get("pages_project"):
        meta["pages_project"] = project
        apps._write_meta(conn, project_id, "apps/cloudflare.json", meta)
    return {"deployment": dep, "url": url, "files": len(dist)}


def pages_url(project: str) -> str:
    return f"https://{project}.pages.dev"
