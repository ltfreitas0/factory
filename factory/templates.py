"""Scaffold templates: catalog + write-into-repo.

Templates live as directories under `factory/templates/<id>/`. Scaffolding
copies the template files into the project's repo store via `files.put` so
the sandbox sync and snapshot flows see them without a git checkout.
"""

from __future__ import annotations

from pathlib import Path

from factory import files

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class TemplateError(ValueError):
    pass


def catalog() -> list[dict]:
    out = []
    if not TEMPLATES_DIR.is_dir():
        return out
    for entry in sorted(TEMPLATES_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        readme = entry / "README.md"
        summary = ""
        if readme.is_file():
            first = next((ln for ln in readme.read_text().splitlines() if ln.strip()), "")
            summary = first.lstrip("# ").strip()
        out.append({"id": entry.name, "summary": summary})
    return out


def scaffold(conn, project_id: str, template_id: str) -> int:
    """Copy template files into the project's repo store. Returns file count."""
    src = TEMPLATES_DIR / template_id
    if not src.is_dir():
        raise TemplateError(f"unknown template: {template_id}")
    if not (src / "package.json").is_file():
        raise TemplateError(f"template {template_id} has no package.json")
    n = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.name in {"README.md", ".gitignore"}:
            continue
        if any(part in {"node_modules", "dist", ".git"} for part in path.relative_to(src).parts):
            continue
        rel = path.relative_to(src).as_posix()
        if path.suffix in {".png", ".jpg", ".svg", ".ico"}:
            files.put_bytes(conn, project_id, rel, path.read_bytes(), mime="image/svg+xml" if path.suffix == ".svg" else "")
        else:
            files.put(conn, project_id, rel, path.read_text())
        n += 1
    # record the template on the project row
    conn.execute(
        "UPDATE projects SET template_id = ? WHERE id = ?", (template_id, project_id)
    )
    conn.commit()
    return n
