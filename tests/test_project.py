import json
import subprocess

import pytest

from factory import files, project
from factory.db import connect


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    return connect()


def test_create_list_patch_delete(db, tmp_path):
    created = project.create(db, slug="atlas", name="Atlas")
    assert created["slug"] == "atlas"
    assert created["name"] == "Atlas"
    assert created["open_tickets"] == 0
    assert (tmp_path / "ws" / "atlas" / ".git").is_dir()

    listed = project.list_projects(db)
    assert any(p["slug"] == "atlas" and p["name"] == "Atlas" for p in listed)

    renamed = project.patch(db, "atlas", name="Atlas Prime")
    assert renamed["name"] == "Atlas Prime"

    project.delete(db, "atlas")
    assert project.get(db, "atlas") is None
    with pytest.raises(KeyError):
        project.delete(db, "atlas")


def test_slugify_and_reject_bad(db):
    assert project.slugify("Hello World") == "hello-world"
    with pytest.raises(ValueError, match="invalid slug"):
        project.create(db, slug="NOPE_underscore", name="x")
    with pytest.raises(ValueError, match="invalid slug"):
        project.create(db, slug="", name="???")


def test_seed_and_cloudflare_no_leak(db):
    p = project.create(db, slug="cfdemo", name="CF")
    project.seed_files(db, p["id"])
    paths = {f["path"] for f in files.list_prefix(db, p["id"], "")}
    assert "pipeline.yml" in paths
    assert "instances/dev.json" in paths

    out = project.connect_cloudflare(
        db,
        "cfdemo",
        account_id="acct123",
        api_token="super-secret-token",
        pages_project="demo-pages",
        r2_bucket="demo-bucket",
    )
    cf = out["connections"]["cloudflare"]
    assert cf["token_set"] is True
    assert cf["account_id"] == "acct123"
    assert cf["pages_project"] == "demo-pages"
    dumped = json.dumps(out)
    assert "super-secret-token" not in dumped
    vault = files.get(db, p["id"], "CF_API_TOKEN", store="vault")
    assert "body" not in vault or not vault.get("body")


def test_connect_git_file_remote(db, tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    project.create(db, slug="gitted", name="Gitted")
    remote = f"file://{bare}"
    out = project.connect_git(db, "gitted", remote)
    assert out["git_remote"] == remote
    assert out["connections"]["git"] == remote
