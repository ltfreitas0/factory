import sqlite3

import pytest

from factory import dispatch, files


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "d.db")
    conn.row_factory = sqlite3.Row
    files.ensure_schema(conn)
    return conn


def test_missing_pipeline(db):
    with pytest.raises(dispatch.DispatchError):
        dispatch.run(db, "p", "dev")


def test_runs_pipeline_file(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path))
    files.put(db, "p", "pipeline.yml", "echo dispatch-ok")
    files.put(db, "p", "instances/dev.json", '{"name":"dev","production":false}')
    out = dispatch.run(db, "p", "dev")
    assert out["ok"] is True
    assert "dispatch-ok" in out["stdout"]


def test_refuses_prod(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path))
    monkeypatch.delenv("FACTORY_ALLOW_PROD", raising=False)
    files.put(db, "p", "pipeline.yml", "echo no")
    files.put(db, "p", "instances/prod.json", '{"production":true}')
    with pytest.raises(dispatch.DispatchError):
        dispatch.run(db, "p", "prod")
