"""In-process live feed. Runner/worker publish; UI subscribes via SSE."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from queue import Queue
from typing import Any

_lock = threading.Lock()
_buf: deque[dict] = deque(maxlen=300)
_subs: list[Queue] = []
_sink = None  # optional persist callback(item)


def publish(
    kind: str,
    text: str,
    *,
    ticket_id: str | None = None,
    state: str | None = None,
    title: str | None = None,
) -> dict:
    item = {
        "at": time.strftime("%H:%M:%S"),
        "kind": kind,
        "text": text,
        "ticket_id": ticket_id,
        "state": state,
        "title": title,
    }
    with _lock:
        _buf.append(item)
        dead: list[Queue] = []
        for q in _subs:
            try:
                q.put_nowait(item)
            except Exception:
                dead.append(q)
        for q in dead:
            _subs.remove(q)
    sink = _sink
    if sink is not None:
        try:
            sink(item)
        except Exception:
            pass
    return item


def set_sink(fn) -> None:
    global _sink
    _sink = fn


def hydrate(items: list[dict]) -> None:
    with _lock:
        _buf.clear()
        for item in items[-300:]:
            _buf.append(item)


def history() -> list[dict]:
    with _lock:
        return list(_buf)


def subscribe() -> Queue:
    q: Queue = Queue(maxsize=200)
    with _lock:
        _subs.append(q)
    return q


def unsubscribe(q: Queue) -> None:
    with _lock:
        if q in _subs:
            _subs.remove(q)


def dump(item: dict) -> str:
    return json.dumps(item, default=str)
