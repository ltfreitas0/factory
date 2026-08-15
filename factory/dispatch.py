"""Run the persisted pipeline file. Never invent steps."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from factory import files, obs

VAULT_REF = re.compile(r"\$\{vault/([A-Za-z0-9._-]+)\}")


class DispatchError(ValueError):
    pass


def _resolve(conn, project_id: str, text: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        # We only store digests in vault — for dispatch, also allow env fallback.
        env_key = name.replace(".", "_")
        val = os.environ.get(env_key) or os.environ.get(name)
        if not val:
            raise DispatchError(f"missing vault/env secret: {name}")
        return val

    return VAULT_REF.sub(repl, text)


def run(conn, project_id: str, instance: str = "dev") -> dict:
    try:
        pipe = files.get(conn, project_id, "pipeline.yml")
    except files.FileError as exc:
        raise DispatchError("no pipeline.yml") from exc
    try:
        inst = files.get(conn, project_id, f"instances/{instance}.json")
        meta = json.loads(inst.get("body") or "{}")
    except (files.FileError, json.JSONDecodeError):
        meta = {"name": instance, "production": instance == "prod"}
    if meta.get("production") and os.environ.get("FACTORY_ALLOW_PROD") != "1":
        # Human/API must set the flag or we refuse — runner never sets it.
        raise DispatchError("refusing production dispatch")
    body = _resolve(conn, project_id, pipe.get("body") or "")
    if not body.strip():
        raise DispatchError("empty pipeline")
    cwd = Path(os.environ.get("FACTORY_ROOT") or ".")
    obs.emit("dispatch_start", project_id=project_id, instance=instance)
    proc = subprocess.run(
        body,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ},
    )
    ok = proc.returncode == 0
    obs.emit(
        "dispatch_end",
        project_id=project_id,
        instance=instance,
        ok=ok,
    )
    return {
        "ok": ok,
        "instance": instance,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
