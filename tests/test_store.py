from pathlib import Path

from factory import store
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
