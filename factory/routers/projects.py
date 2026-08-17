"""Projects: CRUD, workflow, git, Cloudflare connection, apps."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from factory import apps, infra, messages, obs, project
from factory.machine import IllegalTransition as MachineIllegal
from factory.routers._common import _project, db

router = APIRouter()


@router.get("/api/projects")
def projects():
    return project.list_projects(db())


@router.get("/api/projects/{slug}")
def get_project(slug: str):
    row = project.get(db(), slug)
    if not row:
        raise HTTPException(404, "unknown project")
    return row


class ProjectIn(BaseModel):
    name: str
    slug: str | None = None
    validate_cmd: str = "true"
    git_remote: str | None = None
    mode: str = "live"
    quality_gates: bool = False
    template_id: str | None = None


@router.post("/api/projects")
def create_project(body: ProjectIn, request: Request):
    uid = getattr(request.state, "user_id", None)
    try:
        proj = project.create(
            db(),
            slug=body.slug or "",
            name=body.name,
            validate_cmd=body.validate_cmd,
            owner_id=uid,
            mode=body.mode,
            quality_gates=body.quality_gates,
            template_id=body.template_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    project.seed_files(db(), proj["id"])
    infra.ensure_default_branches(db(), proj["id"])
    infra.ensure_default_instances(db(), proj["id"])
    token = messages.rotate_ingest_token(db(), proj["id"])
    if body.git_remote:
        try:
            proj = project.connect_git(db(), proj["slug"], body.git_remote)
        except ValueError as exc:
            raise HTTPException(400, f"created {proj['slug']} but git failed: {exc}") from exc
    obs.emit("project_created", project=proj["slug"], mode=body.mode)
    out = project.get(db(), proj["slug"])
    out["ingest_token"] = token
    return out


class ProjectPatch(BaseModel):
    workflow: list[dict] | None = None
    name: str | None = None
    validate_cmd: str | None = None
    mode: str | None = None
    quality_gates: bool | None = None


@router.patch("/api/projects/{slug}")
def patch_project(slug: str, body: ProjectPatch):
    proj = _project(slug)
    conn = db()
    if body.workflow is not None:
        old = project.workflow_of(proj)
        occ = project.occupied_stage_ids(conn, proj["id"])
        try:
            project.validate_workflow_patch(old, body.workflow, occ)
        except MachineIllegal as exc:
            raise HTTPException(409, str(exc)) from exc
        project.save_workflow(conn, proj["id"], body.workflow)
    if (
        body.name is not None
        or body.validate_cmd is not None
        or body.mode is not None
        or body.quality_gates is not None
    ):
        try:
            project.patch(
                conn,
                slug,
                name=body.name,
                validate_cmd=body.validate_cmd,
                mode=body.mode,
                quality_gates=body.quality_gates,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError:
            raise HTTPException(404, "unknown project") from None
    return project.get(conn, slug)


@router.delete("/api/projects/{slug}")
def delete_project(slug: str):
    try:
        project.delete(db(), slug)
    except KeyError:
        raise HTTPException(404, "unknown project") from None
    obs.emit("project_deleted", project=slug)
    return {"ok": True, "slug": slug}


class GitIn(BaseModel):
    remote: str


@router.post("/api/projects/{slug}/git")
def git_connect(slug: str, body: GitIn):
    try:
        out = project.connect_git(db(), slug, body.remote)
    except KeyError:
        raise HTTPException(404, "unknown project") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    obs.emit("git_connected", project=slug)
    return out


class CloudflareIn(BaseModel):
    account_id: str = ""
    api_token: str = ""
    pages_project: str = ""
    r2_bucket: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""


@router.post("/api/projects/{slug}/cloudflare")
def cloudflare_connect(slug: str, body: CloudflareIn):
    try:
        out = project.connect_cloudflare(
            db(),
            slug,
            account_id=body.account_id,
            api_token=body.api_token,
            pages_project=body.pages_project,
            r2_bucket=body.r2_bucket,
            r2_access_key_id=body.r2_access_key_id,
            r2_secret_access_key=body.r2_secret_access_key,
        )
    except KeyError:
        raise HTTPException(404, "unknown project") from None
    obs.emit("cloudflare_connected", project=slug)
    return out


class AppInstallIn(BaseModel):
    token: str = ""


class AppBindIn(BaseModel):
    repo: str = ""
    branch: str = ""
    account_id: str = ""
    account_name: str = ""
    pages_project: str = ""
    r2_bucket: str = ""


class OAuthIn(BaseModel):
    code: str
    state: str


@router.get("/api/projects/{slug}/apps")
def project_apps(slug: str):
    proj = _project(slug)
    return apps.list_apps(db(), proj["id"], git_remote=proj.get("git_remote"))


@router.post("/api/projects/{slug}/apps/{app_id}/install")
def install_app(slug: str, app_id: str, body: AppInstallIn):
    proj = _project(slug)
    token = body.token.strip() or None
    if not token and apps.oauth_available(app_id):
        # OAuth-first: when a client is configured, drive the browser flow.
        try:
            started = apps.oauth_start(slug, app_id)
        except apps.AppError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": False, "oauth": True, "url": started["url"]}
    if not token and not apps.env_ready(app_id):
        raise HTTPException(409, "authorize this app to continue")
    try:
        meta = apps.install(db(), proj["id"], app_id, token)
    except apps.AppError as exc:
        raise HTTPException(400, str(exc)) from exc
    obs.emit("app_installed", project=slug, app=app_id)
    return {"ok": True, "app": app_id, "identity": meta.get("identity"), "resource": apps._resource_view(app_id, meta)}


@router.post("/api/projects/{slug}/apps/{app_id}/uninstall")
def uninstall_app(slug: str, app_id: str):
    proj = _project(slug)
    try:
        apps.uninstall(db(), proj["id"], app_id)
    except apps.AppError as exc:
        raise HTTPException(400, str(exc)) from exc
    obs.emit("app_uninstalled", project=slug, app=app_id)
    return {"ok": True, "app": app_id}


@router.get("/api/projects/{slug}/apps/{app_id}/resources")
def app_resources(slug: str, app_id: str):
    proj = _project(slug)
    try:
        return apps.resources(db(), proj["id"], app_id)
    except apps.AppError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/projects/{slug}/apps/{app_id}/bind")
def bind_app(slug: str, app_id: str, body: AppBindIn):
    try:
        meta = apps.bind(db(), slug, app_id, body.model_dump())
    except KeyError:
        raise HTTPException(404, "unknown project") from None
    except (apps.AppError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    obs.emit("app_bound", project=slug, app=app_id)
    return {"ok": True, "app": app_id, "resource": apps._resource_view(app_id, meta)}


@router.get("/api/projects/{slug}/apps/{app_id}/oauth")
def app_oauth(slug: str, app_id: str):
    try:
        return apps.oauth_start(slug, app_id)
    except apps.AppError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/apps/oauth/callback")
def app_oauth_callback(body: OAuthIn):
    try:
        return apps.oauth_finish(db(), body.code, body.state)
    except apps.AppError as exc:
        raise HTTPException(400, str(exc)) from exc
