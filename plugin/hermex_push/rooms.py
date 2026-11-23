"""Which `bot_room` turns become a room message, so the phone pushes only those.

The room hides a pass and the lead's routing answer; the hooks cannot see the room, so the
two checks read what the host puts in the turn: the reply text and the first prompt line.
"""
from __future__ import annotations

import re
from typing import Any

from .payload import _as_text

_PASS = re.compile(r"\(?\s*pass\s*\)?\.?", re.IGNORECASE)


def is_pass(reply: Any) -> bool:
    """Mirror of hermes-agent `gateway/hosted_room_discussion.py` `is_pass_text`."""
    text = str(reply or "").strip()
    return not text or _PASS.fullmatch(text) is not None


def is_lead_dispatch(user_message: Any) -> bool:
    """True for the lead's routing prompt (`gateway/hosted_room_lead.py`), whose answer the room never shows."""
    text = _as_text(user_message)
    return text.startswith('[Discussion: "') and "the lead of this room." in text.split("\n", 1)[0]
