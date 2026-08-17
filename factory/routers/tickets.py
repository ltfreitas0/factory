"""Tickets: list/create/get, SM transitions, documents."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from factory import store
from factory.machine import IllegalTransition as MachineIllegal
from factory.routers._common import db
from factory.sm import IllegalTransition

router = APIRouter()


class TicketIn(BaseModel):
    title: str
    body: str = ""
    project: str = "corpora"
    kind: str = "build"
    source: str = "human"


class TransitionIn(BaseModel):
    to: str
    actor: str = Field(default="human", pattern="^(human|runner)$")


class DocIn(BaseModel):
    kind: str
    body: str
    author: str = "human"


class ActionIn(BaseModel):
    name: str
    actor: str = "human"
    target: str | None = None
    autonomy: bool = False


@router.get("/api/tickets")
def tickets(project: str | None = None):
    return store.list_tickets(db(), project=project)


@router.post("/api/tickets")
def create_ticket(body: TicketIn):
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE slug = ?", (body.project,)).fetchone()
    if not proj:
        raise HTTPException(404, "unknown project")
    return store.create_ticket(
        conn,
        project_id=proj["id"],
        title=body.title,
        body=body.body,
        kind=body.kind,
        source=body.source,
    )


@router.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    t = store.get_ticket(db(), ticket_id)
    if not t:
        raise HTTPException(404, "not found")
    return t


@router.post("/api/tickets/{ticket_id}/action")
def ticket_action(ticket_id: str, body: ActionIn):
    try:
        return store.apply_action(db(), ticket_id, body.model_dump())
    except MachineIllegal as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError:
        raise HTTPException(404, "not found") from None


@router.post("/api/tickets/{ticket_id}/transition")
def do_transition(ticket_id: str, body: TransitionIn):
    try:
        return store.transition(db(), ticket_id, body.to, body.actor)
    except IllegalTransition as e:
        raise HTTPException(409, str(e)) from e
    except KeyError:
        raise HTTPException(404, "not found") from None


@router.post("/api/tickets/{ticket_id}/accept")
def accept(ticket_id: str):
    t = store.get_ticket(db(), ticket_id)
    if not t:
        raise HTTPException(404, "not found")
    dest = "ready_to_validate" if t.get("kind") == "validate" else "ready_to_plan"
    if t["state"] not in ("inbox", "proposed"):
        raise HTTPException(409, f"cannot accept from {t['state']}")
    return do_transition(ticket_id, TransitionIn(to=dest, actor="human"))


@router.post("/api/tickets/{ticket_id}/approve-plan")
def approve_plan(ticket_id: str):
    t = store.get_ticket(db(), ticket_id)
    if not t:
        raise HTTPException(404, "not found")
    if not store.latest_doc(db(), ticket_id, "plan"):
        raise HTTPException(409, "no plan to approve")
    return store.transition(db(), ticket_id, "implementing", "human")


@router.post("/api/tickets/{ticket_id}/approve-merge")
def approve_merge(ticket_id: str):
    return do_transition(ticket_id, TransitionIn(to="integrating", actor="human"))


@router.post("/api/tickets/{ticket_id}/documents")
def write_doc(ticket_id: str, body: DocIn):
    if not store.get_ticket(db(), ticket_id):
        raise HTTPException(404, "not found")
    return store.put_doc(db(), ticket_id, body.kind, body.body, body.author)
