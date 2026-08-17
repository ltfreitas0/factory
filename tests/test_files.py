import sqlite3

import pytest

from factory import files


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "f.db")
    conn.row_factory = sqlite3.Row
    files.ensure_schema(conn)
    return conn


def test_repo_roundtrip(db):
    files.put(db, "p1", "context/spec.md", "# hi")
    got = files.get(db, "p1", "context/spec.md")
    assert got["body"] == "# hi"
    assert got["set"] is True


def test_vault_get_has_no_body(db):
    files.put(db, "p1", "INGEST_TOKEN", "secret-plain", store="vault")
    got = files.get(db, "p1", "INGEST_TOKEN", store="vault")
    assert "body" not in got or got.get("body") in (None, "")
    assert got["set"] is True
    assert "secret-plain" not in str(got)


def test_vault_matches(db):
    files.put(db, "p1", "INGEST_TOKEN", "abc", store="vault")
    assert files.vault_matches(db, "p1", "INGEST_TOKEN", "abc")
    assert not files.vault_matches(db, "p1", "INGEST_TOKEN", "nope")


def test_cross_project_isolated(db):
    files.put(db, "a", "x.md", "A")
    files.put(db, "b", "x.md", "B")
    assert files.get(db, "a", "x.md")["body"] == "A"
    assert files.get(db, "b", "x.md")["body"] == "B"


def test_reject_dotdot(db):
    with pytest.raises(files.FileError):
        files.put(db, "p", "../etc/passwd", "x")
    with pytest.raises(files.FileError):
        files.get(db, "p", "/abs")


def test_prefix_list(db):
    files.put(db, "p", "context/a.md", "1")
    files.put(db, "p", "context/b.md", "2")
    files.put(db, "p", "agents/build.md", "3")
    names = [r["path"] for r in files.list_prefix(db, "p", "context/")]
    assert names == ["context/a.md", "context/b.md"]


def test_delete_repo_file(db):
    files.put(db, "p", "context/a.md", "1")
    files.delete(db, "p", "context/a.md")
    with pytest.raises(files.FileError):
        files.get(db, "p", "context/a.md")


def test_missing_vault(db):
    with pytest.raises(files.FileError):
        files.get(db, "p", "NOPE", store="vault")
