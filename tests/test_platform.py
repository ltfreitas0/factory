"""Platform routes: branches, snapshots, instances, deployments, templates, chat."""

from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi.testclient import TestClient


@contextmanager
def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.delenv("FACTORY_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("SANDBOX_GW_URL", "http://127.0.0.1:9")  # unroutable = tests that need it fail loudly
    monkeypatch.setenv("SANDBOX_GW_TOKEN", "test")
    from factory.api import app

    with TestClient(app) as client:
        yield client


def _make_project(client, slug="demo", **kw):
    r = client.post("/api/projects", json={"name": "Demo", "slug": slug, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_create_project_sets_mode_and_infra(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        p = _make_project(client, mode="tickets", quality_gates=True)
        assert p["mode"] == "tickets"
        assert p["quality_gates"] == 1
        branches = client.get("/api/projects/demo/branches").json()
        assert {b["name"] for b in branches} == {"main", "dev", "prod"}
        instances = client.get("/api/projects/demo/instances").json()
        assert {i["name"] for i in instances} == {"dev", "prod"}
        prod = next(i for i in instances if i["name"] == "prod")
        assert prod["production"] == 1
        assert prod["account"] == "user"


def test_patch_mode_and_gates(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client)
        r = client.patch("/api/projects/demo", json={"mode": "tickets", "quality_gates": True})
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "tickets"
        assert r.json()["quality_gates"] == 1
        assert client.patch("/api/projects/demo", json={"mode": "bogus"}).status_code == 400


def test_branches_programmatic(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client)
        r = client.post("/api/projects/demo/branches", json={"name": "feature-x", "kind": "feature"})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "feature-x"
        # duplicate rejected
        assert client.post("/api/projects/demo/branches", json={"name": "feature-x"}).status_code == 400
        # bad name
        assert client.post("/api/projects/demo/branches", json={"name": "a b"}).status_code == 400


def test_snapshot_flow(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client)
        # put a repo file
        r = client.put("/api/projects/demo/files/context/spec.md",
                       json={"content": "hello world"})
        assert r.status_code in (200, 201)
        snap = client.post("/api/projects/demo/snapshots", json={"message": "v1"}).json()
        assert snap["sha"]
        snaps = client.get("/api/projects/demo/snapshots").json()
        assert len(snaps) == 1
        assert snaps[0]["message"] == "v1"


def test_instances_prod_singleton(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client)
        # a second production instance is illegal
        r = client.post("/api/projects/demo/instances", json={"name": "prod2", "production": True})
        assert r.status_code == 400
        r = client.post("/api/projects/demo/instances", json={"name": "canary", "kind": "workers"})
        assert r.status_code == 200


def test_templates_catalog_and_scaffold(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client)
        cats = client.get("/api/templates").json()
        assert any(t["id"] == "vite-react" for t in cats)
        r = client.post("/api/projects/demo/scaffold", json={"template": "vite-react"})
        assert r.status_code == 200, r.text
        assert r.json()["files"] > 0
        # file landed in repo store
        tree = client.get("/api/projects/demo/files", params={"prefix": "src/"}).json()
        assert any(e["path"] == "src/App.tsx" for e in tree)
        assert client.post("/api/projects/demo/scaffold", json={"template": "nope"}).status_code == 400


def test_chat_tickets_mode(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client, mode="tickets")
        r = client.post("/api/projects/demo/chat", json={"text": "make a thing"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["mode"] == "tickets"
        assert data["ticket"]["title"] == "make a thing"
        # message recorded
        msgs = client.get("/api/projects/demo/messages").json()
        assert any(m["payload"].get("text") == "make a thing" for m in msgs)


def test_chat_live_mode_stubbed_agent(tmp_path, monkeypatch):
    """Live mode: message recorded, agent run executed, sandbox sync attempted."""
    from factory import worker

    if worker.available():
        monkeypatch.setattr(
            worker,
            "run",
            lambda cwd, brief, timeout=600, ticket_id=None, run_id=None: worker.WorkerResult(
                ok=True, stdout="did the thing", stderr="", code=0
            ),
        )
    from factory import sandbox

    if hasattr(sandbox, "sync"):
        monkeypatch.setattr(
            sandbox, "sync", lambda project: {"ok": True, "workspace": f"/workspace/{project}"}
        )
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client, mode="live")
        r = client.post("/api/projects/demo/chat", json={"text": "hello agent"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["mode"] == "live"
        assert data["ok"] is True
        assert data["run"]["status"] == "ok"
        msgs = client.get("/api/projects/demo/messages").json()
        assert any(m["payload"].get("text") == "hello agent" for m in msgs)


def test_chat_live_mode_agent_missing(tmp_path, monkeypatch):
    from factory import worker

    monkeypatch.setattr(worker, "run", lambda cwd, brief, timeout=600, ticket_id=None, run_id=None: (_ for _ in ()).throw(RuntimeError("agent boom")))
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client, mode="live")
        r = client.post("/api/projects/demo/chat", json={"text": "hello agent"})
        assert r.status_code in (500, 502)  # agent path surfaced, message still recorded
        msgs = client.get("/api/projects/demo/messages").json()
        assert any(m["payload"].get("text") == "hello agent" for m in msgs)


def test_sandbox_status_never(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client)
        s = client.get("/api/projects/demo/sandbox").json()
        assert s["status"] == "never"


def test_deploy_dev_fails_cleanly_without_gateway(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        _make_project(client)
        r = client.post("/api/projects/demo/deployments", json={"instance": "dev"})
        # gateway unreachable -> 502 surfaced with sandbox detail, deployment recorded as failed
        assert r.status_code == 502
        deps = client.get("/api/projects/demo/deployments").json()
        assert len(deps) == 1
        assert deps[0]["status"] == "failed"
