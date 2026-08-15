"""Generic ticket machine: kind × status × actor.

Pure. No I/O. Workflow is a list of stage dicts on the project.
Ticket is {stage, status}. See .meta/SHAPE.md §3.
"""

from __future__ import annotations

from copy import deepcopy


KINDS = frozenset({"human", "agent", "plugin"})
STATUSES = frozenset({"ready", "running", "blocked", "done"})
ACTORS = frozenset({"human", "runner", "handler"})
ACTIONS = frozenset({"claim", "succeed", "fail", "advance", "retry", "return", "land"})


class IllegalTransition(ValueError):
    pass


def _stage(workflow: list[dict], sid: str | None) -> dict | None:
    if not sid:
        return None
    for s in workflow:
        if s.get("id") == sid:
            return s
    return None


def _index(workflow: list[dict], sid: str) -> int:
    for i, s in enumerate(workflow):
        if s.get("id") == sid:
            return i
    raise IllegalTransition(f"unknown stage: {sid}")


def first_human(workflow: list[dict]) -> dict | None:
    for s in workflow:
        if s.get("kind") == "human" and not s.get("muted"):
            return s
    return None


def first_machine(workflow: list[dict]) -> dict | None:
    for s in workflow:
        if s.get("kind") in {"agent", "plugin"} and not s.get("muted"):
            return s
    return None


def next_unmuted(workflow: list[dict], sid: str, *, skip_muted_human: bool = False) -> dict | None:
    """Stage after sid, walking over muted agent/plugin. Human muted still stops
    unless skip_muted_human (unused; SHAPE: mute never skips a human gate)."""
    i = _index(workflow, sid)
    for s in workflow[i + 1 :]:
        if s.get("muted") and s.get("kind") != "human":
            continue
        if s.get("muted") and s.get("kind") == "human" and skip_muted_human:
            continue
        return s
    return None


def _require(ticket: dict, status: str | set[str], action: str) -> None:
    ok = {status} if isinstance(status, str) else status
    if ticket.get("status") not in ok:
        raise IllegalTransition(f"cannot {action} from status {ticket.get('status')}")


def apply(workflow: list[dict], ticket: dict, action: dict) -> dict:
    """Return a new ticket dict. Raises IllegalTransition. Never mutates inputs."""
    name = action.get("name")
    actor = action.get("actor")
    if name not in ACTIONS:
        raise IllegalTransition(f"unknown action: {name}")
    if actor not in ACTORS:
        raise IllegalTransition(f"unknown actor: {actor}")
    if ticket.get("status") == "done" and name != "land":
        raise IllegalTransition("done tickets do not move")

    out = deepcopy(ticket)
    if name == "land":
        return _land(workflow, out, action)
    if out.get("status") not in STATUSES:
        raise IllegalTransition(f"bad status: {out.get('status')}")
    st = _stage(workflow, out.get("stage"))
    if st is None:
        raise IllegalTransition(f"ticket stage missing from workflow: {out.get('stage')}")

    if name == "claim":
        return _claim(st, out, actor)
    if name == "succeed":
        return _succeed(workflow, st, out, actor)
    if name == "fail":
        return _fail(st, out, actor)
    if name == "advance":
        return _advance(workflow, st, out, actor)
    if name == "retry":
        return _retry(st, out, actor)
    if name == "return":
        return _return(workflow, st, out, actor, action.get("target"))
    raise IllegalTransition(f"unhandled action: {name}")


def _claim(st: dict, ticket: dict, actor: str) -> dict:
    if actor != "runner":
        raise IllegalTransition("only runner may claim")
    _require(ticket, "ready", "claim")
    if st.get("kind") not in {"agent", "plugin"}:
        raise IllegalTransition("cannot claim a human stage")
    if st.get("muted"):
        raise IllegalTransition("cannot claim a muted stage")
    ticket["status"] = "running"
    return ticket


def _succeed(workflow: list[dict], st: dict, ticket: dict, actor: str) -> dict:
    if actor != "runner":
        raise IllegalTransition("only runner may succeed")
    _require(ticket, "running", "succeed")
    nxt = next_unmuted(workflow, st["id"])
    if nxt is None:
        ticket["status"] = "done"
        return ticket
    ticket["stage"] = nxt["id"]
    ticket["status"] = "ready"
    return ticket


def _fail(st: dict, ticket: dict, actor: str) -> dict:
    if actor != "runner":
        raise IllegalTransition("only runner may fail")
    _require(ticket, "running", "fail")
    ticket["status"] = "blocked"
    return ticket


def _advance(workflow: list[dict], st: dict, ticket: dict, actor: str) -> dict:
    if actor != "human":
        raise IllegalTransition("only human may advance")
    _require(ticket, {"ready", "blocked"}, "advance")
    if st.get("kind") != "human":
        raise IllegalTransition("advance only from a human stage")
    nxt = next_unmuted(workflow, st["id"])
    if nxt is None:
        ticket["status"] = "done"
        return ticket
    ticket["stage"] = nxt["id"]
    ticket["status"] = "ready"
    return ticket


def _retry(st: dict, ticket: dict, actor: str) -> dict:
    if actor != "human":
        raise IllegalTransition("only human may retry")
    _require(ticket, "blocked", "retry")
    ticket["status"] = "ready"
    return ticket


def _return(
    workflow: list[dict], st: dict, ticket: dict, actor: str, target: str | None
) -> dict:
    if actor != "human":
        raise IllegalTransition("only human may return")
    _require(ticket, {"ready", "blocked"}, "return")
    if not target:
        raise IllegalTransition("return requires target")
    dest = _stage(workflow, target)
    if dest is None:
        raise IllegalTransition(f"unknown return stage: {target}")
    here = _index(workflow, st["id"])
    there = _index(workflow, dest["id"])
    if there >= here:
        raise IllegalTransition("return must go to a previous stage")
    if dest.get("muted") and dest.get("kind") != "human":
        raise IllegalTransition("cannot return to a muted machine stage")
    ticket["stage"] = dest["id"]
    ticket["status"] = "ready"
    return ticket


def _land(workflow: list[dict], ticket: dict, action: dict) -> dict:
    if action.get("actor") != "handler":
        raise IllegalTransition("only handler may land")
    named = action.get("target")
    autonomy = bool(action.get("autonomy"))
    if named:
        st = _stage(workflow, named)
        if st is None:
            raise IllegalTransition(f"unknown land stage: {named}")
        if autonomy and st.get("kind") == "human":
            m = first_machine(workflow)
            if m is None:
                raise IllegalTransition("autonomy land needs a machine stage")
            st = m
        ticket["stage"] = st["id"]
        ticket["status"] = "ready"
        return ticket
    if autonomy:
        m = first_machine(workflow)
        if m is None:
            raise IllegalTransition("autonomy land needs a machine stage")
        ticket["stage"] = m["id"]
        ticket["status"] = "ready"
        return ticket
    h = first_human(workflow)
    if h is None:
        raise IllegalTransition("land needs a human stage")
    ticket["stage"] = h["id"]
    ticket["status"] = "ready"
    return ticket
