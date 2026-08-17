from factory import apps
from factory.db import connect
from factory.project import create


def test_catalog_install_bind_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_ID", raising=False)

    def fake_json(method, url, **kwargs):
        if url.endswith("/user"):
            return {"login": "ltfreitas0"}
        if "user/repos" in url:
            return [{"full_name": "ltfreitas0/demo", "private": False, "default_branch": "main"}]
        if url.endswith("/accounts?per_page=50") or url.rstrip("/").endswith("accounts"):
            return {"result": [{"id": "acct-1", "name": "Sandbox"}]}
        if "pages/projects" in url:
            return {"result": [{"name": "demo-pages"}]}
        if "r2/buckets" in url:
            return {"result": {"buckets": [{"name": "demo-bucket"}]}}
        raise AssertionError(url)

    monkeypatch.setattr(apps, "request_json", fake_json)
    conn = connect()
    p = create(conn, slug="apptest", name="App Test")

    listed = apps.list_apps(conn, p["id"])
    assert {a["id"] for a in listed} == {"github", "cloudflare"}
    assert all(not a["installed"] for a in listed)

    try:
        apps.install(conn, p["id"], "github", None)
        assert False
    except apps.AppError:
        pass

    gh = apps.install(conn, p["id"], "github", "gh-secret")
    assert gh["identity"] == "ltfreitas0"
    dumped = str(apps.list_apps(conn, p["id"]))
    assert "gh-secret" not in dumped

    repos = apps.resources(conn, p["id"], "github")
    assert repos["repos"][0]["full_name"] == "ltfreitas0/demo"
    apps.bind(conn, "apptest", "github", {"repo": "ltfreitas0/demo"})
    again = apps.list_apps(conn, p["id"], git_remote=None)
    github = next(a for a in again if a["id"] == "github")
    assert github["resource"]["repo"] == "ltfreitas0/demo"

    cf = apps.install(conn, p["id"], "cloudflare", "cf-secret")
    assert cf["account_id"] == "acct-1"
    apps.bind(conn, "apptest", "cloudflare", {"pages_project": "demo-pages", "r2_bucket": "demo-bucket"})
    cf_app = next(a for a in apps.list_apps(conn, p["id"]) if a["id"] == "cloudflare")
    assert cf_app["resource"]["pages_project"] == "demo-pages"
    assert "cf-secret" not in str(cf_app)

    apps.uninstall(conn, p["id"], "github")
    apps.uninstall(conn, p["id"], "cloudflare")
    after = apps.list_apps(conn, p["id"])
    assert all(not a["installed"] for a in after)


def test_oauth_start_requires_client(monkeypatch):
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_ID", raising=False)
    try:
        apps.oauth_start("x", "github")
        assert False
    except apps.AppError:
        pass
