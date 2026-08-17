import subprocess

from factory import files, tree
from factory.db import connect
from factory.project import create


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_list_and_read_blob(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    conn = connect()
    p = create(conn, slug="treed", name="Treed")
    root = tmp_path / "ws" / "treed"
    (root / "src").mkdir()
    (root / "src" / "hi.md").write_text("# hello\n")
    (root / "src" / "pic.bin").write_bytes(b"\x00\x01\x02")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "files")

    listing = tree.list_tree(root)
    paths = {e["path"] for e in listing["entries"]}
    assert "src/hi.md" in paths
    assert listing["branch"] in {"master", "main"}

    md = tree.read_blob(root, "src/hi.md")
    assert md["kind"] == "markdown"
    assert "# hello" in md["body"]
    assert md["binary"] is False

    raw = tree.read_blob(root, "src/pic.bin")
    assert raw["kind"] == "binary"
    assert raw["binary"] is True
    assert raw["body"] is None


def test_reject_bad_ref_and_path(tmp_path):
    try:
        tree.list_tree(tmp_path / "missing")
        assert False
    except files.FileError:
        pass
    try:
        tree.read_blob(tmp_path, "../etc/passwd")
        assert False
    except files.FileError:
        pass


def test_upload_lists_and_syncs_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("FACTORY_DB", str(tmp_path / "f.db"))
    conn = connect()
    p = create(conn, slug="up", name="Up")
    out = files.put_bytes(conn, p["id"], "docs/note.md", b"# note\n", sync_disk=True)
    assert out["kind"] == "text"
    listed = files.list_prefix(conn, p["id"], "")
    assert any(r["path"] == "docs/note.md" for r in listed)
    assert (tmp_path / "ws" / "up" / "docs" / "note.md").read_text() == "# note\n"
