"""Platform: branches, snapshots, instances, deployments, sandbox, templates, chat."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from factory import feed, infra, messages, obs, project, store
from factory.routers._common import _id, _now_iso, _project, db

router = APIRouter()


# --- dispatch (legacy pipeline.yml runner) -------------------------------

class DispatchIn(BaseModel):
    instance: str = "dev"
    allow_prod: bool = False


@router.post("/api/projects/{slug}/dispatch")
def dispatch_project(slug: str, body: DispatchIn):
    import os

    from factory import deploy

    proj = _project(slug)
    if body.allow_prod:
        os.environ["FACTORY_ALLOW_PROD"] = "1"
    try:
        return deploy.run_pipeline(db(), proj["id"], body.instance)
    except deploy.DeployError as exc:
        raise HTTPException(409, str(exc)) from exc


# --- branches / snapshots / instances / deployments ----------------------

@router.get("/api/projects/{slug}/branches")
def project_branches(slug: str):
    return infra.list_branches(db(), _project(slug)["id"])


class BranchIn(BaseModel):
    name: str
    kind: str = "feature"
    from_sha: str | None = None


@router.post("/api/projects/{slug}/branches")
def create_branch(slug: str, body: BranchIn):
    try:
        return infra.create_branch(
            db(), _project(slug)["id"], body.name, body.kind, body.from_sha
        )
    except infra.InfraError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/projects/{slug}/snapshots")
def project_snapshots(slug: str):
    return infra.list_snapshots(db(), _project(slug)["id"])


class SnapshotIn(BaseModel):
    message: str = ""
    branch: str | None = None


@router.post("/api/projects/{slug}/snapshots")
def create_snapshot(slug: str, body: SnapshotIn):
    proj = _project(slug)
    branch_id = None
    if body.branch:
        b = infra.get_branch(db(), proj["id"], body.branch)
        if b:
            branch_id = b["id"]
    try:
        return infra.create_snapshot(
            db(), proj["id"], message=body.message, branch_id=branch_id, created_by="user"
        )
    except infra.InfraError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/projects/{slug}/instances")
def project_instances(slug: str):
    return infra.list_instances(db(), _project(slug)["id"])


class InstanceIn(BaseModel):
    name: str
    production: bool = False
    kind: str = "sandbox"
    account: str = "platform"
    url: str = ""
    branch: str = ""


@router.post("/api/projects/{slug}/instances")
def create_instance(slug: str, body: InstanceIn):
    try:
        return infra.create_instance(
            db(),
            _project(slug)["id"],
            name=body.name,
            production=body.production,
            kind=body.kind,
            account=body.account,
            url=body.url,
            branch=body.branch,
        )
    except infra.InfraError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/projects/{slug}/deployments")
def project_deployments(slug: str):
    return infra.list_deployments(db(), _project(slug)["id"])


@router.get("/api/deployments/{deployment_id}")
def get_deployment(deployment_id: str):
    d = infra.get_deployment(db(), deployment_id)
    if not d:
        raise HTTPException(404, "not found")
    return d


class DeployIn(BaseModel):
    instance: str = "dev"
    branch: str = ""
    build_cmd: str | None = None


@router.post("/api/projects/{slug}/deployments")
def create_deployment(slug: str, body: DeployIn, request: Request):
    """Deploy current state to an instance. Dev = sandbox preview; prod = human-gated."""
    conn = db()
    proj = _project(slug)
    inst = infra.get_instance(conn, proj["id"], body.instance)
    if not inst:
        raise HTTPException(404, f"unknown instance: {body.instance}")
    uid = getattr(request.state, "user_id", None)
    if inst["production"] and not uid:
        raise HTTPException(403, "production deploy requires a logged-in user")
    sha = _id("sha")
    # deploy point: current state -> immutable snapshot
    try:
        infra.create_snapshot(conn, proj["id"], sha=sha, message=f"deploy {body.instance}",
                              created_by="user")
    except infra.InfraError as exc:
        raise HTTPException(400, f"snapshot failed: {exc}") from exc
    dep = infra.create_deployment(
        conn, proj["id"], instance_id=inst["id"], branch=body.branch or inst["branch"] or "main",
        sha=sha, deployed_by="user",
    )
    # dev instance: provision sandbox + expose preview
    if inst["kind"] == "sandbox":
        try:
            from factory import sandbox

            sandbox.ensure(proj["slug"])
            sandbox.sync(conn, proj["id"], proj["slug"])
            # expose_port derives the preview host from SANDBOX_GW_URL
            # (host:port) so local wrangler dev URLs route correctly.
            url = sandbox.expose_port(proj["slug"], 5173)
            infra.set_deployment_status(conn, dep["id"], "ready", url=url,
                                        infra_ref=proj["slug"])
            sandbox_row = conn.execute(
                "SELECT * FROM sandboxes WHERE project_id = ?", (proj["id"],)
            ).fetchone()
            if sandbox_row:
                conn.execute(
                    "UPDATE sandboxes SET status='running', preview_url=?, last_synced_sha=?, seen_at=? WHERE project_id=?",
                    (url, sha, _now_iso(), proj["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO sandboxes (project_id, sandbox_id, status, instance_type, port, preview_url, last_synced_sha, seen_at)
                       VALUES (?, ?, 'running', 'lite', 5173, ?, ?, ?)""",
                    (proj["id"], proj["slug"], url, sha, _now_iso()),
                )
            conn.commit()
        except Exception as exc:
            infra.set_deployment_status(conn, dep["id"], "failed")
            raise HTTPException(502, f"sandbox provision failed: {exc}") from exc
        obs.emit("deploy_ok", project=proj["slug"], instance=body.instance, sha=sha)
    elif inst["kind"] == "pages":
        # Production Pages deploy: build in the sandbox, direct-upload.
        from factory import deploy

        try:
            out = deploy.deploy_pages(
                conn, proj["id"], proj["slug"],
                account_id=inst.get("account_id") or None,
                build_cmd=body.build_cmd or "bun run build",
                branch=body.branch or inst["branch"] or "main",
            )
            infra.set_deployment_status(
                conn, dep["id"], "ready", url=out["url"], infra_ref=out["deployment"].get("id") or proj["slug"]
            )
            conn.execute(
                "UPDATE instances SET url = ? WHERE id = ?", (out["url"], inst["id"])
            )
            conn.commit()
        except deploy.DeployError as exc:
            infra.set_deployment_status(conn, dep["id"], "failed")
            raise HTTPException(502, f"pages deploy failed: {exc}") from exc
        return infra.get_deployment(conn, dep["id"])
    else:
        obs.emit("deploy_started", project=proj["slug"], instance=body.instance, sha=sha,
                 note="workers/container deploy lands when wired")
    return infra.get_deployment(conn, dep["id"])


# --- sandbox lifecycle ----------------------------------------------------

@router.get("/api/projects/{slug}/sandbox")
def sandbox_status(slug: str):
    row = db().execute(
        "SELECT * FROM sandboxes WHERE project_id = ?", (_project(slug)["id"],)
    ).fetchone()
    if not row:
        return {"status": "never", "project": slug}
    d = dict(row)
    return {"status": d["status"], "preview_url": d.get("preview_url") or "",
            "last_synced_sha": d.get("last_synced_sha") or "", "port": d.get("port")}


@router.post("/api/projects/{slug}/sandbox/ensure")
def sandbox_ensure(slug: str):
    try:
        from factory import sandbox

        return sandbox.ensure(slug)
    except Exception as exc:
        raise HTTPException(502, f"sandbox ensure failed: {exc}") from exc


@router.post("/api/projects/{slug}/sandbox/sync")
def sandbox_sync(slug: str):
    try:
        from factory import sandbox

        proj = _project(slug)
        return sandbox.sync(db(), proj["id"], slug)
    except Exception as exc:
        raise HTTPException(502, f"sandbox sync failed: {exc}") from exc


@router.post("/api/projects/{slug}/sandbox/sleep")
def sandbox_sleep(slug: str):
    try:
        from factory import sandbox

        out = sandbox.sleep(slug)
        db().execute(
            "UPDATE sandboxes SET status='sleeping' WHERE project_id = ?",
            (_project(slug)["id"],),
        )
        db().commit()
        return out
    except Exception as exc:
        raise HTTPException(502, f"sandbox sleep failed: {exc}") from exc


@router.post("/api/projects/{slug}/sandbox/destroy")
def sandbox_destroy(slug: str):
    try:
        from factory import sandbox

        out = sandbox.destroy(slug)
        db().execute(
            "UPDATE sandboxes SET status='destroyed' WHERE project_id = ?",
            (_project(slug)["id"],),
        )
        db().commit()
        return out
    except Exception as exc:
        raise HTTPException(502, f"sandbox destroy failed: {exc}") from exc


# --- templates + chat -----------------------------------------------------

@router.get("/api/templates")
def templates():
    import factory.templates as tmpl

    return tmpl.catalog()


@router.post("/api/projects/{slug}/scaffold")
def scaffold_project(slug: str, body: dict):
    template_id = (body or {}).get("template") or "vite-react"
    import factory.templates as tmpl

    proj = _project(slug)
    try:
        n = tmpl.scaffold(db(), proj["id"], template_id)
    except tmpl.TemplateError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "template": template_id, "files": n}


class ChatIn(BaseModel):
    text: str
    mode: str | None = None  # override project.mode for this turn


@router.post("/api/projects/{slug}/chat")
def chat(slug: str, body: ChatIn, request: Request):
    """Live-mode agent turn: message -> run. Tickets mode wraps in a ticket."""
    from factory import sandbox

    conn = db()
    proj = _project(slug)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    mode = body.mode or proj.get("mode") or "live"
    if mode not in ("live", "tickets"):
        raise HTTPException(400, f"bad mode: {mode}")
    msg = messages.add(
        conn, proj["id"], source="user", payload={"text": text, "mode": mode},
    )
    if mode == "tickets":
        ticket = store.create_ticket(
            conn, project_id=proj["id"], title=text[:80], body=text, source="human"
        )
        obs.emit("chat_ticket", project=proj["slug"], ticket=ticket["id"])
        return {"mode": "tickets", "message": msg, "ticket": ticket}
    # live mode: agent runs in the project worktree, then sync to sandbox
    run = store.start_run(conn, None, "implement", project_id=proj["id"])
    worktree = project.workspace_root() / proj["slug"]
    brief = f"Work in {worktree}. User request: {text}. Edit files directly, then summarize what changed."
    try:
        from factory import worker

        result = worker.run(worktree, brief, ticket_id=None, run_id=run["id"])
    except Exception as exc:
        store.finish_run(conn, run["id"], ok=False, stdout="", stderr=str(exc))
        feed.publish("stderr", f"agent run failed: {exc}", run_id=run["id"])
        raise HTTPException(502, f"agent run failed: {exc}") from exc
    store.finish_run(conn, run["id"], ok=result.ok, stdout=result.stdout, stderr=result.stderr)
    # token usage + cost summary (jarvis pattern: usage surfaces at turn end)
    try:
        from factory import cost as cost_mod

        usage = cost_mod.attach_run(conn, run["id"], "", run["started_at"])
        if usage and usage.get("tokens"):
            feed.publish(
                "usage",
                f"turn done · {usage['tokens']} tokens · ${cost_mod.usd(usage.get('usage') or {}):.4f}",
                run_id=run["id"],
            )
    except Exception:
        pass
    sync_out = {}
    if result.ok:
        try:
            sync_out = sandbox.sync(conn, proj["id"], proj["slug"])
        except Exception as exc:
            obs.emit("sandbox_sync_fail", project=proj["slug"], message=str(exc))
    obs.emit("chat_turn", project=proj["slug"], ok=result.ok, run=run["id"])
    return {
        "mode": "live",
        "message": msg,
        "run": store.get_run(conn, run["id"]),
        "sandbox_sync": sync_out,
        "ok": result.ok,
    }
