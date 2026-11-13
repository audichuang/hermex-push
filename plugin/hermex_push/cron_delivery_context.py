"""Fork-only bridge for Hermes v0.23.1's scheduler delivery boundary.

The scheduler facade is also used by durable queue draining. Keep trusted job identity
local to that call; asyncio.to_thread copies this context into the standalone sender.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import RLock


@dataclass(frozen=True)
class DeliveryContext:
    job_id: str
    job_name: str
    execution_id: str


_current: ContextVar[DeliveryContext | None] = ContextVar("hermex_cron_delivery", default=None)
_lock = RLock()
_leases = 0
_wrapper = None
_original = None
_scheduler = None


def current_delivery() -> DeliveryContext | None:
    return _current.get()


def install():
    """Acquire one profile's lease; the final release restores only our own wrapper."""
    global _leases, _wrapper, _original, _scheduler
    from importlib import import_module

    with _lock:
        if _leases == 0:
            scheduler = import_module("cron.scheduler")
            original = scheduler._deliver_result

            @wraps(original)
            def deliver(job, content, *args, **kwargs):
                token = _current.set(DeliveryContext(
                    job_id=str(job.get("id") or ""),
                    job_name=str(job.get("name") or job.get("id") or ""),
                    execution_id=str(job.get("execution_id") or ""),
                ))
                try:
                    return original(job, content, *args, **kwargs)
                finally:
                    _current.reset(token)

            _scheduler, _original, _wrapper = scheduler, original, deliver
            scheduler._deliver_result = deliver
        _leases += 1
    released = False

    def release():
        nonlocal released
        global _leases, _wrapper, _original, _scheduler
        with _lock:
            if released:
                return
            released = True
            _leases -= 1
            if _leases == 0:
                if _scheduler._deliver_result is _wrapper:
                    _scheduler._deliver_result = _original
                _scheduler = _original = _wrapper = None

    return release
