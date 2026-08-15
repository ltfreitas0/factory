from pathlib import Path

from factory import cost, store
from factory.db import connect
from factory.sm import IllegalTransition


def test_ticket_lifecycle(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    proj = store.ensure_playground(conn, str(tmp_path / "repo"))
    t = store.create_ticket(conn, project_id=proj["id"], title="add hello", body="print hi")
    assert t["state"] == "inbox"
    t = store.transition(conn, t["id"], "ready_to_plan", "human")
    assert t["state"] == "ready_to_plan"
    t = store.transition(conn, t["id"], "planning", "runner")
    store.put_doc(conn, t["id"], "plan", "1. write hello", "worker")
    t = store.transition(conn, t["id"], "plan_review", "runner")
    t = store.transition(conn, t["id"], "implementing", "human")
    assert t["state"] == "implementing"
    plan = store.latest_doc(conn, t["id"], "plan")
    assert plan["body"] == "1. write hello"
    try:
        store.transition(conn, t["id"], "done", "human")
        raise AssertionError("skipping gates should fail")
    except IllegalTransition:
        pass


def test_list_tickets_filters_project(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    play = store.ensure_playground(conn, str(tmp_path / "play"))
    corp = store.ensure_project(conn, "corpora", str(tmp_path / "corp"), "scripts/validate")
    store.create_ticket(conn, project_id=play["id"], title="play ticket", body="")
    store.create_ticket(conn, project_id=corp["id"], title="corp ticket", body="")
    only = store.list_tickets(conn, project="corpora")
    assert [t["title"] for t in only] == ["corp ticket"]
    assert only[0]["project"] == "corpora"


def test_factory_spawn_is_proposed_and_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    corp = store.ensure_project(conn, "corpora", str(tmp_path / "corp"), "true")
    meta = tmp_path / "corp" / ".meta"
    meta.mkdir(parents=True)
    (meta / "spawn.json").write_text(
        '{"tickets":[{"title":"gap: create project","body":"CRUD project","kind":"build"}]}'
    )
    made = store.spawn_from_repo(conn, project_id=corp["id"], repo=tmp_path / "corp")
    assert len(made) == 1
    assert made[0]["state"] == "proposed"
    assert made[0]["source"] == "factory"
    again = store.spawn_from_repo(conn, project_id=corp["id"], repo=tmp_path / "corp")
    assert again == []


def test_usd_from_usage():
    n = cost.usd({"input": 1_000_000, "output": 0, "cache_read": 0, "reasoning": 0})
    assert abs(n - cost.rates()["input"]) < 1e-9


def test_validate_ticket_skips_plan(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    corp = store.ensure_project(conn, "corpora", str(tmp_path / "corp"), "true")
    t = store.create_ticket(
        conn, project_id=corp["id"], title="e2e", body="run suite", kind="validate"
    )
    assert t["kind"] == "validate"
    t = store.transition(conn, t["id"], "ready_to_validate", "human")
    t = store.transition(conn, t["id"], "validating", "runner")
    t = store.transition(conn, t["id"], "done", "runner")
    assert t["state"] == "done"


def test_apply_action_advance(tmp_path: Path):
    conn = connect(tmp_path / "ax.db")
    proj = store.ensure_project(conn, "demo", str(tmp_path), "true")
    t = store.create_ticket(conn, project_id=proj["id"], title="x", body="")
    assert t["stage"] == "inbox"
    nxt = store.apply_action(conn, t["id"], {"name": "advance", "actor": "human"})
    assert nxt["stage"] == "planning"
    assert nxt["status"] == "ready"
    assert nxt["state"] == "ready_to_plan"
