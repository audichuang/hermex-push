"""Explicit platform delivery, independent of turn hooks and the gateway's lifetime.

A delivery snapshot is a normal host session, not an invented cron run. Keeping it in the
active Profile's SessionDB lets existing phones open long script output as well as agent
results without putting plaintext or host filesystem paths through the relay.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from .hooks import bot_name_for, relay_url_from_env
from .keys import load_or_create_keys
from .payload import notify_event, preview
from .cron_delivery_context import current_delivery
from .compat import compatibility_event
from .relay import RelaySender, notify_url


MEDIA_NOTICE = "Attachments are not supported in Hermex; only text was delivered."


def parse_target(raw: str):
    return (raw, None) if raw == "default" else None


def _redact_name(name: str) -> str:
    if not name:
        return name
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(name, force=True)
    except Exception:
        return "[REDACTED - redaction failed]"


def _content(message: str, execution: Any, metadata: dict) -> tuple[str, str]:
    # Hermes adds this wrapper on BOTH live and standalone cron sends (wrap_response=true).
    wrapped = re.fullmatch(
        r'Cronjob Response: (.*?)\n\(job_id: [^\n]*\)\n-------------\n\n(.*)\n\n'
        r'To stop or manage this job, send me a new message .*', message, re.S)
    trusted = current_delivery()
    if trusted is not None:
        return _redact_name(trusted.job_name), wrapped[2] if wrapped else message
    if wrapped:
        return _redact_name(wrapped[1]), wrapped[2]
    if execution is not None:
        return _redact_name(execution.job_name), message
    if metadata.get("job_id"):
        from cron.jobs import get_job
        job = get_job(str(metadata["job_id"]))
        return _redact_name(str((job or {}).get("name") or metadata["job_id"])), message
    return "", message


def _deliver(chat_id: str, message: str, metadata: dict, media_files: Any) -> dict:
    if chat_id != "default":
        return {"error": "Hermex delivery target is default"}
    if not message.strip() and not media_files:
        return {"error": "Hermex cannot deliver empty text"}
    url = relay_url_from_env()
    if not url:
        return {"error": "HERMEX_PUSH_RELAY_URL is not configured"}
    from hermes_cli.profiles import current_profile_name
    from hermes_state_registry import acquire
    try:
        from cron.execution_identity import current_cron_execution
        execution = current_cron_execution()
    except ImportError:  # pre-0.23 Hermes has no execution observer
        execution = None
    profile = current_profile_name("default") or "default"
    keys = load_or_create_keys()
    task_name, body = _content(message, execution, metadata)
    banner_body = body
    if media_files:
        banner_body = MEDIA_NOTICE + "\n\n" + body
        body = (body.rstrip() + "\n\n" + MEDIA_NOTICE).lstrip()
    name = bot_name_for(profile)
    kind = "delivery"
    sid = "hermex-delivery-" + uuid.uuid4().hex
    # Persist BEFORE notifying. A DB failure is a failed delivery, never a dead tap.
    db = acquire()
    try:
        db.create_session(sid, source="gui")
        db.set_session_title(sid, f"{(task_name or name)[:80]} · {sid[-8:]}")
        db.append_message(sid, role="assistant", content=body)
        db.end_session(sid, "hermex_delivery")
    finally:
        db.close()
    event = notify_event(kind=kind, session_id=sid, event_ref=sid, source="other", is_subagent=False,
                         keys=keys, preview=preview(
                             title=f"{name} · Delivery",
                             bot_name=name, subtitle=task_name, body=banner_body,
                             profile="" if profile == "default" else profile))
    return _transmit(sid, event, keys, url)


def _transmit(sid, event, keys, url):
    sender = RelaySender()
    result = []
    if not sender.deliver(notify_url(url, keys.install_key), event, result.append):
        return {"error": "Hermex relay did not acknowledge delivery; the saved output remains on the host"}
    if result == ["ignored"]:
        # V1 relays explicitly ignored the new kind BEFORE fan-out. Reuse this snapshot
        # and its seal as a reply, so a plugin-only upgrade still reaches App Store 2.1.
        legacy = compatibility_event(event)
        if not sender.deliver(notify_url(url, keys.install_key), legacy):
            return {"error": "Hermex relay did not acknowledge the compatible delivery"}
    return {"success": True, "message_id": sid}


async def standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None,
                          force_document=False, notify=None, metadata=None):
    """The standard Hermes platform sender contract; returns only after relay acknowledgement."""
    try:
        return await asyncio.to_thread(_deliver, chat_id, message, metadata or {}, media_files)
    except Exception:
        # Exceptions can contain host paths, message content or credentials.
        return {"error": "Hermex could not persist or send the delivery"}
