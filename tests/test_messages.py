import sqlite3

import pytest

from factory import files, messages

WF = [
    {"id": "inbox", "kind": "human"},
    {"id": "build", "kind": "agent"},
]


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    messages.ensure_schema(conn)
    return conn


def test_ingest_requires_token(db):
    messages.rotate_ingest_token(db, "p")
    with pytest.raises(messages.AuthError):
        messages.ingest(db, "p", None, "app", {"x": 1})
    with pytest.raises(messages.AuthError):
        messages.ingest(db, "p", "wrong", "app", {"x": 1})


def test_ingest_ok_and_rotate_kills_old(db):
    tok = messages.rotate_ingest_token(db, "p")
    msg = messages.ingest(db, "p", tok, "forum", {"note": "hi"})
    assert msg["source"] == "forum"
    assert msg["payload"] == {"note": "hi"}
    assert msg["handled_at"] is None
    new = messages.rotate_ingest_token(db, "p")
    with pytest.raises(messages.AuthError):
        messages.ingest(db, "p", tok, "forum", {"note": "nope"})
    msg2 = messages.ingest(db, "p", new, "forum", {"note": "ok"})
    assert msg2["payload"]["note"] == "ok"


def test_token_is_one_to_one(db):
    messages.rotate_ingest_token(db, "a")
    tb = messages.rotate_ingest_token(db, "b")
    with pytest.raises(messages.AuthError):
        messages.ingest(db, "a", tb, "app", {})


def test_handler_autonomy_false_lands_inbox(db):
    tok = messages.rotate_ingest_token(db, "p")
    files.put(db, "p", "handlers/fb.yml", "source: forum\nautonomy: false\n")
    msg = messages.ingest(db, "p", tok, "forum", {"text": "please"})
    out = messages.process(db, "p", msg["id"], WF)
    assert out["handled_at"]
    assert out["result"]["ticket"]["stage"] == "inbox"
    assert out["result"]["autonomy"] is False


def test_handler_autonomy_true_lands_build(db):
    tok = messages.rotate_ingest_token(db, "p")
    files.put(db, "p", "handlers/err.yml", "source: runtime\nautonomy: true\n")
    msg = messages.ingest(db, "p", tok, "runtime", {"level": "error"})
    out = messages.process(db, "p", msg["id"], WF)
    assert out["result"]["ticket"]["stage"] == "build"


def test_no_handler_drops(db):
    tok = messages.rotate_ingest_token(db, "p")
    msg = messages.ingest(db, "p", tok, "x", {})
    out = messages.process(db, "p", msg["id"], WF)
    assert out["result"]["drop"] is True
    assert "ticket" not in out["result"]


def test_process_idempotent(db):
    tok = messages.rotate_ingest_token(db, "p")
    files.put(db, "p", "handlers/a.yml", "autonomy: false\n")
    msg = messages.ingest(db, "p", tok, "x", {})
    a = messages.process(db, "p", msg["id"], WF)
    b = messages.process(db, "p", msg["id"], WF)
    assert a["handled_at"] == b["handled_at"]
