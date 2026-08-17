"""Read a project checkout as a git tree. Isolated from tickets."""

from __future__ import annotations

import mimetypes
import subprocess
from pathlib import Path

from factory.files import FileError, normalize

REF_OK = __import__("re").compile(r"^[A-Za-z0-9._/-]+$")
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}
TEXT_CAP = 400_000
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp"}


def _safe_ref(ref: str) -> str:
    ref = (ref or "HEAD").strip() or "HEAD"
    if ref.startswith("-") or not REF_OK.match(ref):
        raise FileError(f"illegal ref: {ref!r}")
    return ref


def _git(root: Path, *args: str, raw: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=not raw,
    )


def current_branch(root: Path) -> str:
    if not (root / ".git").exists():
        return "worktree"
    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if r.returncode != 0:
        return "HEAD"
    return (r.stdout or "HEAD").strip() or "HEAD"


def list_tree(repo_path: str | Path, ref: str = "HEAD") -> dict:
    root = Path(repo_path)
    if not root.is_dir():
        raise FileError("no checkout")
    ref = _safe_ref(ref)
    branch = current_branch(root)
    if (root / ".git").exists():
        r = _git(root, "ls-tree", "-r", "--long", ref)
        if r.returncode != 0:
            raise FileError((r.stderr or "git ls-tree failed")[:300])
        entries = []
        for line in (r.stdout or "").splitlines():
            if "\t" not in line:
                continue
            meta, path = line.split("\t", 1)
            parts = meta.split()
            size = 0
            if len(parts) >= 4 and parts[3].isdigit():
                size = int(parts[3])
            entries.append({"path": path, "type": "blob", "size": size})
        return {"branch": branch, "ref": ref, "entries": entries}
    entries = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        entries.append({"path": rel, "type": "blob", "size": p.stat().st_size})
    return {"branch": branch, "ref": "worktree", "entries": entries}


def kind_of(path: str, binary: bool) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if binary:
        return "binary"
    if ext in {".md", ".markdown"}:
        return "markdown"
    if ext == ".json":
        return "json"
    return "text"


def mime_of(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def read_blob(repo_path: str | Path, path: str, ref: str = "HEAD") -> dict:
    root = Path(repo_path)
    path = normalize(path)
    ref = _safe_ref(ref)
    data = _read_bytes(root, path, ref)
    binary = _is_binary(data)
    body = None
    if not binary:
        text = data.decode("utf-8", errors="replace")
        if len(text) > TEXT_CAP:
            text = text[:TEXT_CAP] + "\n… truncated\n"
        body = text
    return {
        "path": path,
        "ref": ref,
        "kind": kind_of(path, binary),
        "mime": mime_of(path),
        "bytes": len(data),
        "binary": binary,
        "body": body,
    }


def blob_bytes(repo_path: str | Path, path: str, ref: str = "HEAD") -> tuple[bytes, str]:
    root = Path(repo_path)
    path = normalize(path)
    ref = _safe_ref(ref)
    return _read_bytes(root, path, ref), mime_of(path)


def _read_bytes(root: Path, path: str, ref: str) -> bytes:
    if not root.is_dir():
        raise FileError("no checkout")
    if (root / ".git").exists() and ref != "worktree":
        spec = f"{ref}:{path}"
        r = _git(root, "show", spec, raw=True)
        if r.returncode != 0:
            # fall through to working tree
            disk = root / path
            if disk.is_file():
                return disk.read_bytes()
            err = (r.stderr or b"git show failed").decode("utf-8", errors="replace")
            raise FileError(err[:300])
        return r.stdout or b""
    disk = root / path
    if not disk.is_file():
        raise FileError(f"not found: {path}")
    return disk.read_bytes()


def _is_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data[:8192]:
        return True
    sample = data[:8192]
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False
