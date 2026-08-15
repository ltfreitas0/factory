"""Ticket state machine. Human vs runner edges are explicit."""

from __future__ import annotations

STATES = (
    "inbox",
    "ready_to_plan",
    "planning",
    "plan_review",
    "implementing",
    "validating",
    "pr_open",
    "merge_review",
    "integrating",
    "done",
    "needs_human",
    "failed",
)

# (from, to, actor) — actor is "human" or "runner"
EDGES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("inbox", "ready_to_plan", "human"),
        ("inbox", "needs_human", "human"),
        ("ready_to_plan", "planning", "runner"),
        ("planning", "plan_review", "runner"),
        ("planning", "failed", "runner"),
        ("plan_review", "implementing", "human"),
        ("plan_review", "ready_to_plan", "human"),
        ("plan_review", "needs_human", "human"),
        ("implementing", "validating", "runner"),
        ("implementing", "failed", "runner"),
        ("implementing", "needs_human", "runner"),
        ("validating", "pr_open", "runner"),
        ("validating", "implementing", "runner"),
        ("validating", "failed", "runner"),
        ("pr_open", "merge_review", "runner"),
        ("merge_review", "integrating", "human"),
        ("merge_review", "implementing", "human"),
        ("merge_review", "needs_human", "human"),
        ("integrating", "done", "runner"),
        ("integrating", "failed", "runner"),
        ("failed", "ready_to_plan", "human"),
        ("failed", "implementing", "human"),
        ("needs_human", "ready_to_plan", "human"),
        ("needs_human", "implementing", "human"),
        ("needs_human", "inbox", "human"),
    }
)

AUTO_FROM = {
    "ready_to_plan": "planning",
    "pr_open": "merge_review",
}


class IllegalTransition(ValueError):
    pass


def can(src: str, dst: str, actor: str) -> bool:
    return (src, dst, actor) in EDGES


def transition(src: str, dst: str, actor: str) -> str:
    if not can(src, dst, actor):
        raise IllegalTransition(f"{actor} cannot {src} → {dst}")
    return dst


def auto_next(src: str) -> str | None:
    return AUTO_FROM.get(src)
