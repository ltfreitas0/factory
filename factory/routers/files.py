"""Files: repo/vault CRUD, git tree/blob, upload, ingest, messages."""

from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from factory import files, messages, obs, tree
from factory.messages import AuthError
from factory.routers._common import _project, db

router = APIRouter()


@router.get("/api/projects/{slug}/files")
def list_files(slug: str, prefix: str = "", store: str = "repo"):
    return files.list_prefix(db(), _project(slug)["id"], prefix, store)


@router.get("/api/projects/{slug}/tree")
def project_tree(slug: str, ref: str = "HEAD"):
    proj = _project(slug)
    try:
        return tree.list_tree(proj["repo_path"], ref)
    except files.FileError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/projects/{slug}/tree/{path:path}")
def project_tree_file(slug: str, path: str, ref: str = "HEAD"):
    proj = _project(slug)
    try:
        return tree.read_blob(proj["repo_path"], path, ref)
    except files.FileError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/projects/{slug}/blob/{path:path}")
def project_blob(slug: str, path: str, ref: str = "HEAD"):
    proj = _project(slug)
    try:
        data, mime = tree.blob_bytes(proj["repo_path"], path, ref)
    except files.FileError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(content=data, media_type=mime)


@router.post("/api/projects/{slug}/files/upload")
async def upload_file(
    slug: str,
    file: UploadFile = File(...),
    path: str = Form(""),
    store: str = Form("repo"),
):
    proj = _project(slug)
    dest = (path or file.filename or "upload.bin").strip().replace("\\", "/")
    data = await file.read()
    try:
        out = files.put_bytes(
            db(),
            proj["id"],
            dest,
            data,
            store=store,
            mime=file.content_type or "",
            sync_disk=store == "repo",
        )
    except files.FileError as exc:
        raise HTTPException(400, str(exc)) from exc
    obs.emit("asset_uploaded", project=slug, path=dest, bytes=len(data))
    return out


@router.post("/api/projects/{slug}/ingest-token")
def mint_ingest(slug: str):
    """Rotate the project's only ingest token. Plaintext is returned once."""
    proj = _project(slug)
    token = messages.rotate_ingest_token(db(), proj["id"])
    obs.emit("ingest_token_rotated", project=slug)
    return {"token": token, "once": True}


class IngestIn(BaseModel):
    source: str = "app"
    payload: dict = Field(default_factory=dict)


@router.post("/ingest/{slug}/messages")
def ingest_message(slug: str, body: IngestIn, authorization: str | None = Header(default=None)):
    proj = _project(slug)
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    try:
        msg = messages.ingest(db(), proj["id"], token, body.source, body.payload)
    except AuthError:
        raise HTTPException(401, "invalid ingest token") from None

    wf = []
    raw = proj.get("workflow")
    if raw:
        try:
            wf = json.loads(raw)
        except json.JSONDecodeError:
            wf = []
    if not wf:
        wf = [
            {"id": "inbox", "kind": "human"},
            {"id": "build", "kind": "agent"},
        ]
    processed = messages.process(db(), proj["id"], msg["id"], wf)
    obs.emit(
        "message_ingested",
        project=slug,
        message_id=msg["id"],
        source=body.source,
        dropped=bool((processed.get("result") or {}).get("drop")),
    )
    return processed


@router.get("/api/projects/{slug}/messages")
def project_messages(slug: str):
    return messages.list_messages(db(), _project(slug)["id"])


@router.put("/api/projects/{slug}/files/{path:path}")
def put_file(slug: str, path: str, body: dict):
    proj = _project(slug)
    store_name = body.get("store") or "repo"
    try:
        return files.put(db(), proj["id"], path, body.get("body") or "", store=store_name)
    except files.FileError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/projects/{slug}/files/{path:path}")
def get_file(slug: str, path: str, store: str = "repo"):
    proj = _project(slug)
    try:
        return files.get(db(), proj["id"], path, store=store)
    except files.FileError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/api/projects/{slug}/files/{path:path}")
def delete_file(slug: str, path: str, store: str = "repo"):
    proj = _project(slug)
    try:
        files.delete(db(), proj["id"], path, store=store)
    except files.FileError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "path": path, "store": store}
