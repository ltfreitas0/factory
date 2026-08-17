"""Project apps. GitHub and Cloudflare are installable surfaces, not forms.

State is a file (`apps/<id>.json`). Tokens live in the vault. HTTP never
returns a secret. The catalog is the only list of apps — append there.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from factory import files, project

CATALOG = [
    {
        "id": "github",
        "title": "GitHub",
        "summary": "Repositories, branches, pull requests.",
        "vault": "GITHUB_TOKEN",
        "file": "apps/github.json",
    },
    {
        "id": "cloudflare",
        "title": "Cloudflare",
        "summary": "Pages, R2, and the account they live on.",
        "vault": "CF_API_TOKEN",
        "file": "apps/cloudflare.json",
    },
]

_PENDING: dict[str, dict] = {}
_LIVE: dict[str, str] = {}

CF_OAUTH_AUTH_URL = "https://dash.cloudflare.com/oauth2/auth"
CF_OAUTH_TOKEN_URL = "https://dash.cloudflare.com/oauth2/token"
# Scope names = Cloudflare API token permission names. The dev client is
# permissive; these cover Pages/R2/Workers/D1 writes.
CF_OAUTH_SCOPES = (
    "account:read account:write user:read zone:read zone:write "
    "workers:write workers:scripts:write pages:write r2:read r2:write d1:read d1:write"
)


class AppError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _spec(app_id: str) -> dict:
    for item in CATALOG:
        if item["id"] == app_id:
            return item
    raise AppError(f"unknown app: {app_id}")


def _read_meta(conn, project_id: str, path: str) -> dict:
    try:
        raw = files.get(conn, project_id, path)
        data = json.loads(raw.get("body") or "{}")
        return data if isinstance(data, dict) else {}
    except (files.FileError, json.JSONDecodeError, TypeError):
        return {}


def _write_meta(conn, project_id: str, path: str, meta: dict) -> None:
    files.put(conn, project_id, path, json.dumps(meta, indent=2) + "\n")


def _has_vault(conn, project_id: str, name: str) -> bool:
    try:
        files.get(conn, project_id, name, store="vault")
        return True
    except files.FileError:
        return False


def _token(conn, project_id: str, app_id: str) -> str:
    cached = _LIVE.get(f"{project_id}:{app_id}")
    if cached:
        return cached
    env_key = "GITHUB_TOKEN" if app_id == "github" else "CLOUDFLARE_API_TOKEN"
    return os.environ.get(env_key) or os.environ.get(_spec(app_id)["vault"]) or ""


def oauth_available(app_id: str) -> bool:
    if app_id == "github":
        return bool(os.environ.get("GITHUB_OAUTH_CLIENT_ID"))
    if app_id == "cloudflare":
        # CF dashboard OAuth app. Permissive-scope dev client; token lands in
        # the vault like an API token would, so install() is unchanged.
        return bool(os.environ.get("CF_OAUTH_CLIENT_ID") and os.environ.get("CF_OAUTH_CLIENT_SECRET"))
    return False


def env_ready(app_id: str) -> bool:
    if app_id == "github":
        return bool(os.environ.get("GITHUB_TOKEN"))
    if app_id == "cloudflare":
        return bool(os.environ.get("CLOUDFLARE_API_TOKEN"))
    return False


def list_apps(conn, project_id: str, *, git_remote: str | None = None) -> list[dict]:
    out = []
    for spec in CATALOG:
        meta = _read_meta(conn, project_id, spec["file"])
        installed = bool(meta.get("installed")) or _has_vault(conn, project_id, spec["vault"])
        if spec["id"] == "github" and git_remote and not installed:
            installed = True
            meta = {**meta, "repo": _repo_from_remote(git_remote)}
        resource = _resource_view(spec["id"], meta, git_remote=git_remote)
        out.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "summary": spec["summary"],
                "installed": installed,
                "oauth_available": oauth_available(spec["id"]),
                "env_ready": env_ready(spec["id"]),
                "identity": meta.get("identity") or "",
                "resource": resource,
            }
        )
    return out


def _repo_from_remote(remote: str) -> str:
    remote = (remote or "").rstrip("/")
    if remote.endswith(".git"):
        remote = remote[:-4]
    if "github.com/" in remote:
        return remote.split("github.com/", 1)[1]
    if "github.com:" in remote:
        return remote.split("github.com:", 1)[1]
    return remote


def _resource_view(app_id: str, meta: dict, *, git_remote: str | None = None) -> dict:
    if app_id == "github":
        repo = meta.get("repo") or (_repo_from_remote(git_remote) if git_remote else "")
        return {"repo": repo, "branch": meta.get("branch") or ""}
    if app_id == "cloudflare":
        return {
            "account_id": meta.get("account_id") or "",
            "account_name": meta.get("account_name") or "",
            "pages_project": meta.get("pages_project") or "",
            "r2_bucket": meta.get("r2_bucket") or "",
        }
    return {}


def install(conn, project_id: str, app_id: str, token: str | None = None) -> dict:
    spec = _spec(app_id)
    raw = (token or "").strip() or _token(conn, project_id, app_id)
    if not raw:
        raise AppError("not authorized")
    if app_id == "github":
        me = github_me(raw)
        files.put(conn, project_id, spec["vault"], raw, store="vault")
        _LIVE[f"{project_id}:{app_id}"] = raw
        meta = {
            "installed": True,
            "installed_at": _now(),
            "identity": me.get("login") or "",
            "repo": _read_meta(conn, project_id, spec["file"]).get("repo") or "",
            "branch": _read_meta(conn, project_id, spec["file"]).get("branch") or "",
        }
        _write_meta(conn, project_id, spec["file"], meta)
        return meta
    if app_id == "cloudflare":
        accounts = cf_accounts(raw)
        files.put(conn, project_id, spec["vault"], raw, store="vault")
        _LIVE[f"{project_id}:{app_id}"] = raw
        prev = _read_meta(conn, project_id, spec["file"])
        chosen = prev.get("account_id") or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or ""
        name = prev.get("account_name") or os.environ.get("CLOUDFLARE_ACCOUNT_NAME") or ""
        if accounts and not chosen:
            chosen = accounts[0]["id"]
            name = accounts[0]["name"]
        elif accounts and chosen:
            name = next((a["name"] for a in accounts if a["id"] == chosen), name)
        meta = {
            "installed": True,
            "installed_at": _now(),
            "identity": name or chosen,
            "account_id": chosen,
            "account_name": name,
            "pages_project": prev.get("pages_project") or "",
            "r2_bucket": prev.get("r2_bucket") or "",
        }
        _write_meta(conn, project_id, spec["file"], meta)
        _sync_cf_instance(conn, project_id, meta)
        return meta
    raise AppError(f"unknown app: {app_id}")


def uninstall(conn, project_id: str, app_id: str) -> None:
    spec = _spec(app_id)
    _LIVE.pop(f"{project_id}:{app_id}", None)
    try:
        files.delete(conn, project_id, spec["file"])
    except files.FileError:
        pass
    try:
        files.delete(conn, project_id, spec["vault"], store="vault")
    except files.FileError:
        pass
    if app_id == "github":
        # keep the checkout; only drop the app link
        proj = conn.execute("SELECT slug FROM projects WHERE id = ?", (project_id,)).fetchone()
        if proj:
            conn.execute("UPDATE projects SET git_remote = NULL WHERE id = ?", (project_id,))
            conn.commit()
    if app_id == "cloudflare":
        try:
            files.delete(conn, project_id, "instances/cloudflare.json")
        except files.FileError:
            pass


def bind(conn, slug: str, app_id: str, resource: dict) -> dict:
    proj = project.get(conn, slug)
    if not proj:
        raise KeyError(slug)
    spec = _spec(app_id)
    meta = _read_meta(conn, proj["id"], spec["file"])
    if not meta.get("installed") and not _has_vault(conn, proj["id"], spec["vault"]):
        raise AppError("app is not installed")
    if app_id == "github":
        repo = (resource.get("repo") or "").strip().strip("/")
        if not repo or "/" not in repo:
            raise AppError("pick a repository")
        remote = f"https://github.com/{repo}.git"
        project.connect_git(conn, slug, remote)
        meta["installed"] = True
        meta["repo"] = repo
        meta["branch"] = (resource.get("branch") or meta.get("branch") or "").strip()
        _write_meta(conn, proj["id"], spec["file"], meta)
        return meta
    if app_id == "cloudflare":
        if resource.get("account_id"):
            meta["account_id"] = resource["account_id"].strip()
        if resource.get("account_name"):
            meta["account_name"] = resource["account_name"].strip()
        if "pages_project" in resource:
            meta["pages_project"] = (resource.get("pages_project") or "").strip()
        if "r2_bucket" in resource:
            meta["r2_bucket"] = (resource.get("r2_bucket") or "").strip()
        meta["installed"] = True
        meta["identity"] = meta.get("account_name") or meta.get("account_id") or meta.get("identity")
        _write_meta(conn, proj["id"], spec["file"], meta)
        _sync_cf_instance(conn, proj["id"], meta)
        return meta
    raise AppError(f"unknown app: {app_id}")


def resources(conn, project_id: str, app_id: str) -> dict:
    _spec(app_id)
    token = _token(conn, project_id, app_id)
    if not token:
        raise AppError("not authorized")
    if app_id == "github":
        return {"repos": github_repos(token)}
    if app_id == "cloudflare":
        accounts = cf_accounts(token)
        meta = _read_meta(conn, project_id, "apps/cloudflare.json")
        account_id = meta.get("account_id") or (accounts[0]["id"] if accounts else "")
        pages = cf_pages(token, account_id) if account_id else []
        buckets = cf_r2(token, account_id) if account_id else []
        return {"accounts": accounts, "pages": pages, "buckets": buckets, "account_id": account_id}
    return {}


def oauth_start(slug: str, app_id: str) -> dict:
    _spec(app_id)
    if not oauth_available(app_id):
        raise AppError("oauth not configured")
    state = secrets.token_urlsafe(24)
    _PENDING[state] = {"slug": slug, "app": app_id, "at": time.time()}
    if app_id == "github":
        client_id = os.environ["GITHUB_OAUTH_CLIENT_ID"]
        redirect = os.environ.get("GITHUB_OAUTH_REDIRECT_URI") or "http://localhost:5510/"
        q = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "scope": "repo read:user",
                "state": state,
            }
        )
        return {"url": f"https://github.com/login/oauth/authorize?{q}", "state": state}
    if app_id == "cloudflare":
        client_id = os.environ["CF_OAUTH_CLIENT_ID"]
        redirect = cf_oauth_redirect()
        q = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "scope": CF_OAUTH_SCOPES,
                "state": state,
                "response_type": "code",
            }
        )
        return {"url": f"{CF_OAUTH_AUTH_URL}?{q}", "state": state}
    raise AppError("oauth not configured")


def cf_oauth_redirect() -> str:
    return (
        os.environ.get("CF_OAUTH_REDIRECT_URI")
        or "http://localhost:5510/api/auth/cloudflare/callback"
    )


def cf_oauth_exchange(code: str) -> str:
    """Swap the authorization code for a Cloudflare access token."""
    client_id = os.environ.get("CF_OAUTH_CLIENT_ID") or ""
    secret = os.environ.get("CF_OAUTH_CLIENT_SECRET") or ""
    data = request_json(
        "POST",
        CF_OAUTH_TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": cf_oauth_redirect(),
        },
    )
    token = data.get("access_token")
    if not token:
        raise AppError("cloudflare oauth did not return a token")
    return token


def oauth_finish(conn, code: str, state: str) -> dict:
    pending = _PENDING.pop(state, None)
    if not pending or time.time() - pending["at"] > 900:
        raise AppError("oauth state expired")
    app_id = pending["app"]
    slug = pending["slug"]
    if app_id == "github":
        token = github_exchange(code)
        proj = project.get(conn, slug)
        if not proj:
            raise AppError("unknown project")
        install(conn, proj["id"], "github", token)
        return {"slug": slug, "app": app_id}
    if app_id == "cloudflare":
        token = cf_oauth_exchange(code)
        proj = project.get(conn, slug)
        if not proj:
            raise AppError("unknown project")
        install(conn, proj["id"], "cloudflare", token)
        return {"slug": slug, "app": app_id}
    raise AppError("oauth not configured")


def _sync_cf_instance(conn, project_id: str, meta: dict) -> None:
    files.put(
        conn,
        project_id,
        "instances/cloudflare.json",
        json.dumps(
            {
                "account_id": meta.get("account_id") or "",
                "pages_project": meta.get("pages_project") or "",
                "r2_bucket": meta.get("r2_bucket") or "",
            },
            indent=2,
        )
        + "\n",
    )


def request_json(method: str, url: str, *, headers: dict | None = None, data: dict | None = None) -> dict:
    body = None
    hdrs = {"User-Agent": "factory", "Accept": "application/json", **(headers or {})}
    if data is not None:
        raw = urllib.parse.urlencode(data).encode()
        body = raw
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise AppError(f"upstream {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AppError(f"upstream unreachable: {exc.reason}") from exc
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AppError("upstream returned non-json") from exc


def github_me(token: str) -> dict:
    return request_json(
        "GET",
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}"},
    )


def github_repos(token: str) -> list[dict]:
    data = request_json(
        "GET",
        "https://api.github.com/user/repos?per_page=100&sort=updated",
        headers={"Authorization": f"Bearer {token}"},
    )
    if not isinstance(data, list):
        return []
    return [
        {
            "full_name": r.get("full_name"),
            "private": bool(r.get("private")),
            "default_branch": r.get("default_branch") or "main",
        }
        for r in data
        if r.get("full_name")
    ]


def github_exchange(code: str) -> str:
    client_id = os.environ.get("GITHUB_OAUTH_CLIENT_ID") or ""
    secret = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET") or ""
    redirect = os.environ.get("GITHUB_OAUTH_REDIRECT_URI") or "http://localhost:5510/"
    data = request_json(
        "POST",
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": secret,
            "code": code,
            "redirect_uri": redirect,
        },
    )
    token = data.get("access_token")
    if not token:
        raise AppError("github oauth did not return a token")
    return token


def cf_accounts(token: str) -> list[dict]:
    data = request_json(
        "GET",
        "https://api.cloudflare.com/client/v4/accounts?per_page=50",
        headers={"Authorization": f"Bearer {token}"},
    )
    rows = (data.get("result") if isinstance(data, dict) else None) or []
    return [{"id": a.get("id"), "name": a.get("name") or a.get("id")} for a in rows if a.get("id")]


def cf_pages(token: str, account_id: str) -> list[dict]:
    data = request_json(
        "GET",
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/pages/projects",
        headers={"Authorization": f"Bearer {token}"},
    )
    rows = (data.get("result") if isinstance(data, dict) else None) or []
    return [{"name": r.get("name"), "subdomain": r.get("subdomain")} for r in rows if r.get("name")]


def cf_r2(token: str, account_id: str) -> list[dict]:
    data = request_json(
        "GET",
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets",
        headers={"Authorization": f"Bearer {token}"},
    )
    result = data.get("result") if isinstance(data, dict) else None
    rows = []
    if isinstance(result, dict):
        rows = result.get("buckets") or []
    elif isinstance(result, list):
        rows = result
    return [{"name": r.get("name")} for r in rows if r.get("name")]
