"""Compatibility only after an older relay explicitly ignores a new notification kind."""
from __future__ import annotations


def compatibility_event(event: dict) -> dict | None:
    kind = {"delivery": "reply"}.get(event.get("kind"))
    # The ignored request had no fan-out: retain every identifier, timestamp and seal.
    return {**event, "kind": kind} if kind else None
