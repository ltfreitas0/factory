"""Optional board auth. Open locally; FACTORY_AUTH_TOKEN required when set."""

from __future__ import annotations

import hmac
import os


def required() -> bool:
    return bool(os.environ.get("FACTORY_AUTH_TOKEN"))


def check(presented: str | None) -> bool:
    expected = os.environ.get("FACTORY_AUTH_TOKEN")
    if not expected:
        return True
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)
