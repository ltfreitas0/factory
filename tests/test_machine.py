"""Isolated SM tests. No I/O. SHAPE.md §6.1."""

import pytest

from factory.machine import IllegalTransition, apply, first_human, first_machine, next_unmuted

WF = [
    {"id": "inbox", "title": "Inbox", "kind": "human"},
    {"id": "build", "title": "Build", "kind": "agent", "file": "agents/build.md"},
    {"id": "check", "title": "Check", "kind": "plugin", "plugin": "validate"},
    {"id": "dev", "title": "Dev", "kind": "plugin", "plugin": "dispatch", "instance": "dev"},
]


def tkt(stage="inbox", status="ready"):
    return {"stage": stage, "status": status}


def go(ticket, name, actor, **extra):
    return apply(WF, ticket, {"name": name, "actor": actor, **extra})


def test_first_helpers():
    assert first_human(WF)["id"] == "inbox"
    assert first_machine(WF)["id"] == "build"


def test_human_advance_inbox_to_build():
    out = go(tkt(), "advance", "human")
    assert out == {"stage": "build", "status": "ready"}


def test_runner_cannot_advance():
    with pytest.raises(IllegalTransition):
        go(tkt(), "advance", "runner")


def test_runner_cannot_claim_human():
    with pytest.raises(IllegalTransition):
        go(tkt(), "claim", "runner")


def test_human_cannot_claim_or_succeed():
    with pytest.raises(IllegalTransition):
        go(tkt("build"), "claim", "human")
    with pytest.raises(IllegalTransition):
        go(tkt("build", "running"), "succeed", "human")


def test_claim_succeed_walks_to_check():
    out = go(tkt("build"), "claim", "runner")
    assert out["status"] == "running"
    out = go(out, "succeed", "runner")
    assert out == {"stage": "check", "status": "ready"}


def test_succeed_last_stage_is_done():
    out = go(tkt("dev", "running"), "succeed", "runner")
    assert out["status"] == "done"


def test_fail_stays_on_stage():
    out = go(tkt("build", "running"), "fail", "runner")
    assert out == {"stage": "build", "status": "blocked"}


def test_retry_unblocks():
    out = go(tkt("build", "blocked"), "retry", "human")
    assert out == {"stage": "build", "status": "ready"}


def test_runner_cannot_retry():
    with pytest.raises(IllegalTransition):
        go(tkt("build", "blocked"), "retry", "runner")


def test_return_to_previous():
    out = go(tkt("check", "blocked"), "return", "human", target="build")
    assert out == {"stage": "build", "status": "ready"}


def test_return_forward_illegal():
    with pytest.raises(IllegalTransition):
        go(tkt("build", "blocked"), "return", "human", target="dev")


def test_done_does_not_move():
    with pytest.raises(IllegalTransition):
        go(tkt("dev", "done"), "retry", "human")


def test_land_no_autonomy_first_human():
    out = apply(WF, {}, {"name": "land", "actor": "handler", "autonomy": False})
    assert out == {"stage": "inbox", "status": "ready"}


def test_land_autonomy_first_machine():
    out = apply(WF, {}, {"name": "land", "actor": "handler", "autonomy": True})
    assert out == {"stage": "build", "status": "ready"}


def test_land_named_machine():
    out = apply(
        WF, {}, {"name": "land", "actor": "handler", "target": "check", "autonomy": False}
    )
    assert out["stage"] == "check"


def test_land_named_human_with_autonomy_skips_to_machine():
    out = apply(
        WF, {}, {"name": "land", "actor": "handler", "target": "inbox", "autonomy": True}
    )
    assert out["stage"] == "build"


def test_land_unknown_stage():
    with pytest.raises(IllegalTransition):
        apply(WF, {}, {"name": "land", "actor": "handler", "target": "nope"})


def test_muted_plugin_skipped_on_succeed():
    wf = [
        {"id": "inbox", "kind": "human"},
        {"id": "build", "kind": "agent"},
        {"id": "check", "kind": "plugin", "muted": True},
        {"id": "dev", "kind": "plugin"},
    ]
    out = apply(wf, tkt("build", "running"), {"name": "succeed", "actor": "runner"})
    assert out == {"stage": "dev", "status": "ready"}


def test_muted_human_not_skipped():
    wf = [
        {"id": "inbox", "kind": "human"},
        {"id": "review", "kind": "human", "muted": True},
        {"id": "build", "kind": "agent"},
    ]
    out = apply(wf, tkt(), {"name": "advance", "actor": "human"})
    assert out["stage"] == "review"
    nxt = next_unmuted(wf, "inbox")
    assert nxt["id"] == "review"


def test_inputs_not_mutated():
    raw = tkt("build")
    apply(WF, raw, {"name": "claim", "actor": "runner"})
    assert raw == tkt("build")
