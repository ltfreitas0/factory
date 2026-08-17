from fastapi.testclient import TestClient

from factory.api import app


def test_cors_preflight_reaches_protected_route():
    with TestClient(app) as client:
        r = client.options(
            "/api/projects",
            headers={
                "Origin": "https://factory.henosis.cc",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert r.status_code in (200, 204)
        assert r.headers.get("access-control-allow-origin")


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


def test_projects_crud_git_cf_assets(tmp_path, monkeypatch):
    import subprocess
    import uuid

    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    slug = f"p{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        listed = client.get("/api/projects")
        assert listed.status_code == 200
        slugs = {p["slug"] for p in listed.json()}
        assert "playground" in slugs
        assert "corpora" in slugs

        created = client.post(
            "/api/projects",
            json={"name": "Demo Product", "slug": slug, "validate_cmd": "true"},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["slug"] == slug
        assert body["name"] == "Demo Product"
        assert body["ingest_token"]
        assert body["file_count"] >= 1
        token = body["ingest_token"]

        renamed = client.patch(f"/api/projects/{slug}", json={"name": "Demo Two"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Demo Two"

        assets = client.get(f"/api/projects/{slug}/files")
        assert assets.status_code == 200
        paths = {f["path"] for f in assets.json()}
        assert "pipeline.yml" in paths

        put = client.put(
            f"/api/projects/{slug}/files/context/spec.md",
            json={"body": "# spec\n", "store": "repo"},
        )
        assert put.status_code == 200
        got = client.get(f"/api/projects/{slug}/files/context/spec.md")
        assert got.status_code == 200
        assert got.json()["body"] == "# spec\n"

        gone = client.delete(f"/api/projects/{slug}/files/context/spec.md")
        assert gone.status_code == 200
        missing = client.get(f"/api/projects/{slug}/files/context/spec.md")
        assert missing.status_code == 404

        cf = client.post(
            f"/api/projects/{slug}/cloudflare",
            json={
                "account_id": "acct-1",
                "api_token": "cf-secret-value",
                "pages_project": "demo",
            },
        )
        assert cf.status_code == 200, cf.text
        assert cf.json()["connections"]["cloudflare"]["token_set"] is True
        assert cf.json()["connections"]["cloudflare"]["account_id"] == "acct-1"
        assert "cf-secret-value" not in cf.text
        leaked = client.get(f"/api/projects/{slug}/files/CF_API_TOKEN?store=vault")
        assert leaked.status_code == 200
        assert "cf-secret-value" not in leaked.text
        assert token not in leaked.text

        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        git = client.post(
            f"/api/projects/{slug}/git",
            json={"remote": f"file://{bare}"},
        )
        assert git.status_code == 200, git.text
        assert git.json()["git_remote"] == f"file://{bare}"

        deleted = client.delete(f"/api/projects/{slug}")
        assert deleted.status_code == 200
        assert client.get(f"/api/projects/{slug}").status_code == 404


def test_apps_catalog():
    with TestClient(app) as client:
        listed = client.get("/api/projects/playground/apps")
        assert listed.status_code == 200
        ids = {a["id"] for a in listed.json()}
        assert ids == {"github", "cloudflare"}
        for a in listed.json():
            assert "installed" in a
            assert "summary" in a


def test_tree_and_asset_upload():
    with TestClient(app) as client:
        listing = client.get("/api/projects/playground/tree")
        assert listing.status_code == 200
        body = listing.json()
        assert "entries" in body
        assert "branch" in body
        up = client.post(
            "/api/projects/playground/files/upload",
            files={"file": ("note.md", b"# n\n", "text/markdown")},
            data={"path": "docs/note.md", "store": "repo"},
        )
        assert up.status_code == 200, up.text
        assets = client.get("/api/projects/playground/files")
        assert any(x["path"] == "docs/note.md" for x in assets.json())
        blob = client.get("/api/projects/playground/tree/docs/note.md")
        assert blob.status_code == 200
        assert "# n" in (blob.json().get("body") or "")
        client.delete("/api/projects/playground/files/docs/note.md")


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
