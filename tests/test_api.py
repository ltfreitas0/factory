from fastapi.testclient import TestClient

from factory.api import app


def test_ingest_token_one_to_one():
    with TestClient(app) as client:
        minted = client.post("/api/projects/playground/ingest-token")
        assert minted.status_code == 200
        token = minted.json()["token"]
        assert token
        # vault get must not echo the token
        leaked = client.get("/api/projects/playground/files/INGEST_TOKEN?store=vault")
        assert leaked.status_code in (200, 404)
        if leaked.status_code == 200:
            assert token not in leaked.text
        ok = client.post(
            "/ingest/playground/messages",
            json={"source": "forum", "payload": {"note": "hi"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ok.status_code == 200
        assert ok.json()["payload"]["note"] == "hi"
        bad = client.post(
            "/ingest/playground/messages",
            json={"source": "forum", "payload": {"note": "no"}},
            headers={"Authorization": "Bearer wrong"},
        )
        assert bad.status_code == 401
        rotated = client.post("/api/projects/playground/ingest-token")
        old = client.post(
            "/ingest/playground/messages",
            json={"source": "forum", "payload": {"note": "old"}},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert old.status_code == 401
        fresh = client.post(
            "/ingest/playground/messages",
            json={"source": "forum", "payload": {"note": "new"}},
            headers={"Authorization": f"Bearer {rotated.json()['token']}"},
        )
        assert fresh.status_code == 200


def test_health_and_ticket_accept():
    with TestClient(app) as client:
        h = client.get("/api/health")
        assert h.status_code == 200
        assert h.json()["ok"] is True
        created = client.post(
            "/api/tickets",
            json={"title": "say hi", "body": "print hello", "project": "playground"},
        )
        assert created.status_code == 200
        tid = created.json()["id"]
        acc = client.post(f"/api/tickets/{tid}/accept")
        assert acc.status_code == 200
        assert acc.json()["state"] == "ready_to_plan"
