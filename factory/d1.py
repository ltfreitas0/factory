"""D1-backed connection with the same surface as sqlite3.

Emulates the narrow sqlite3 API the factory uses (execute/executescript/
commit/row_factory/dict-style rows) over the Cloudflare D1 HTTP API so
`factory/db.py` can swap backends without touching callers.

Enable with env:
    D1_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN (or CLOUDFLARE_API_TOKEN)
Each statement is one HTTP call; `execute` fetches all rows eagerly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request


class D1Error(ValueError):
    pass


class D1Row:
    """dict-like row supporting row['col'] and row[0] (sqlite3.Row parity)."""

    __slots__ = ("_data", "_keys")

    def __init__(self, data: dict):
        self._data = data
        self._keys = list(data)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._keys[key]]
        return self._data[key]

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def __contains__(self, key) -> bool:
        return key in self._data

    def keys(self):
        return self._keys

    def values(self):
        return [self._data[k] for k in self._keys]

    def items(self):
        return [(k, self._data[k]) for k in self._keys]

    def __iter__(self):
        return iter(self.values())

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"<D1Row {self._data}>"


class D1Cursor:
    def __init__(self, rows: list[D1Row]):
        self._rows = rows
        self._i = 0
        self.description = None

    def fetchone(self) -> D1Row | None:
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def fetchall(self) -> list[D1Row]:
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def __iter__(self):
        return iter(self._rows)


class D1Connection:
    def __init__(
        self,
        account_id: str,
        database_id: str,
        token: str,
        *,
        base: str = "https://api.cloudflare.com/client/v4",
    ):
        self.account_id = account_id
        self.database_id = database_id
        self.token = token
        self.base = base.rstrip("/")
        self.row_factory = None  # rows are always dict-like

    # -- transport -------------------------------------------------------

    def _url(self) -> str:
        return (
            f"{self.base}/accounts/{self.account_id}/d1/database/{self.database_id}/query"
        )

    def _post(self, statements: list[dict]) -> list[dict]:
        body = json.dumps(statements).encode()
        req = urllib.request.Request(
            self._url(),
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "factory-d1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise D1Error(f"d1 http {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise D1Error(f"d1 unreachable: {exc.reason}") from exc
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise D1Error("d1 returned non-json") from exc
        if not data.get("success"):
            errs = data.get("errors") or []
            detail = ""
            for block in data.get("result") or []:
                if block.get("error"):
                    detail = block["error"]
                    break
            msg = errs[0].get("message") if errs else (detail or "unknown")
            raise D1Error(f"d1 error: {msg}")
        return data.get("result") or []

    def _query(self, sql: str, params: tuple | list = ()) -> list[D1Row]:
        stmt: dict = {"sql": sql}
        if params:
            stmt["params"] = [json.dumps(p) if isinstance(p, (dict, list)) else p for p in params]
        results = self._post([stmt])
        rows = []
        for block in results:
            for r in block.get("results") or []:
                rows.append(D1Row(r))
        return rows

    # -- sqlite3-compatible surface ---------------------------------------

    def execute(self, sql: str, params: tuple | list = ()) -> D1Cursor:
        return D1Cursor(self._query(sql, params))

    def executescript(self, script: str) -> None:
        # DDL only (no embedded ';' in strings in our schemas)
        for stmt in script.split(";"):
            stmt = stmt.strip()
            if stmt:
                self._post([{"sql": stmt}])

    def executemany(self, sql: str, seq) -> None:
        for params in seq:
            self._query(sql, params)

    def commit(self) -> None:
        pass  # every statement is its own transaction over HTTP

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass

    def cursor(self) -> "D1Connection":
        return self


def configured() -> bool:
    return bool(
        os.environ.get("D1_ACCOUNT_ID")
        and os.environ.get("D1_DATABASE_ID")
        and (os.environ.get("D1_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN"))
    )


def connect() -> D1Connection:
    token = os.environ.get("D1_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN") or ""
    return D1Connection(
        os.environ["D1_ACCOUNT_ID"],
        os.environ["D1_DATABASE_ID"],
        token,
        base=os.environ.get("D1_BASE") or "https://api.cloudflare.com/client/v4",
    )


def to_sqlite_row(row) -> "sqlite3.Row | dict":
    return row
