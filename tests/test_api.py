from fastapi.testclient import TestClient

from factory.api import app


def test_health_and_ticket_accept():
    with TestClient(app) as client:
        h = client.get("/api/health")
        assert h.status_code == 200
        assert h.json()["ok"] is True
        created = client.post("/api/tickets", json={"title": "say hi", "body": "print hello"})
        assert created.status_code == 200
        tid = created.json()["id"]
        acc = client.post(f"/api/tickets/{tid}/accept")
        assert acc.status_code == 200
        assert acc.json()["state"] == "ready_to_plan"
