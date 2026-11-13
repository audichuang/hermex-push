"""Keep multiplex satellites on Hermex's standalone delivery lane.

Hermes treats a live satellite adapter as an owned transport. Owning only Hermex
also prevents that profile from borrowing the primary's Discord adapter.
"""
from __future__ import annotations

from functools import wraps
import sys
from threading import RLock
from types import SimpleNamespace

from . import PLATFORM_NAME


_MARK = "_hermex_push_transport_scope"
_lock = RLock()


def _scope_helpers():
    from agent.secret_scope import is_multiplex_active
    from hermes_constants import get_hermes_home, get_routing_process_hermes_home

    return is_multiplex_active, get_hermes_home, get_routing_process_hermes_home


def _is_satellite(helpers) -> bool:
    active, current_home, launch_home = helpers
    return active() and current_home().resolve() != launch_home().resolve()


def is_multiplex_satellite() -> bool:
    """Use the launch home, including a named primary, rather than the default home."""
    try:
        helpers = _scope_helpers()
    except (ImportError, AttributeError):
        # Older hosts keep the original factory behavior; register logs the missing bridge.
        return False
    return _is_satellite(helpers)


def install():
    """Lease the host credential gate; the last release restores only our wrapper."""
    # register() runs in a plugin worker while gateway.run may wait for that worker.
    # Importing it here would wait on the startup thread's import lock until load times out.
    host = sys.modules.get("gateway.run")
    if host is None:
        return lambda: None
    if (not hasattr(host, "_platform_has_bot_credential")
            and getattr(getattr(host, "__spec__", None), "_initializing", False)):
        return lambda: None
    # A complete host missing this private seam is incompatible; register warns and
    # keeps the factory gate. A later satellite registration can lease a ready host.
    original = host._platform_has_bot_credential
    helpers = _scope_helpers()
    with _lock:
        state = getattr(host, _MARK, None)
        if state is None:
            # Host-owned state survives plugin/module reloads and all profile copies.
            state = SimpleNamespace(lock=RLock(), leases=0, wrapper=None, original=None)
            setattr(host, _MARK, state)
    with state.lock:
        if state.leases == 0:
            original = host._platform_has_bot_credential

            @wraps(original)
            def has_credential(platform, platform_config, *args, **kwargs):
                if getattr(platform, "value", None) == PLATFORM_NAME and _is_satellite(helpers):
                    return False
                return original(platform, platform_config, *args, **kwargs)

            state.original, state.wrapper = original, has_credential
            host._platform_has_bot_credential = has_credential
        state.leases += 1
    released = False

    def release():
        nonlocal released
        with state.lock:
            if released:
                return
            released = True
            state.leases -= 1
            if state.leases == 0:
                if host._platform_has_bot_credential is state.wrapper:
                    host._platform_has_bot_credential = state.original
                state.original = state.wrapper = None

    return release
