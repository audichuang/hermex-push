"""One-way Hermex platform: explicit deliveries persist full output, then send a sealed banner."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import PLATFORM_NAME, RELAY_URL_ENV
from .hooks import relay_url_from_env
from .delivery import standalone_send, parse_target
from .transport_scope import is_multiplex_satellite

logger = logging.getLogger("hermex_push")


def check_requirements() -> bool:
    """Passive probe: stdlib plus cryptography, which hermes-agent already pins."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def is_connected(config: Any = None) -> bool:
    return bool(relay_url_from_env())


def env_enablement() -> Optional[dict]:
    """Auto-enable the platform when the relay URL is set, so ``gateway status`` shows it."""
    url = relay_url_from_env()
    return {"relay_url": url} if url else None


def make_adapter(config: Any):
    """``adapter_factory``; imports gateway modules lazily so the plugin loads without them."""
    # Keep satellites unowned even if a host no longer exposes the credential gate.
    if is_multiplex_satellite():
        return None
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class HermexAdapter(BasePlatformAdapter):
        # The router must pass full output; we persist it and clip only the banner.
        splits_long_messages = True

        def __init__(self, cfg: Any) -> None:
            super().__init__(config=cfg, platform=Platform(PLATFORM_NAME))

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            if not relay_url_from_env():
                logger.warning("[%s] %s not configured", self.name, RELAY_URL_ENV)
                return False
            self._mark_connected()
            return True

        async def disconnect(self) -> None:
            self._mark_disconnected()

        async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> SendResult:
            if metadata and (metadata.get("interim") or metadata.get("_interim_send")):
                return SendResult(success=True)
            if metadata and metadata.get("job_id"):
                # Cron's live send lacks its media list. Defer before any I/O so the
                # scheduler's standalone lane can persist text + one attachment note.
                return SendResult(success=False, error="Hermex cron delivery uses the standalone sender")
            result = await standalone_send(self.config, chat_id, content, metadata=metadata)
            return SendResult(success=bool(result.get("success")), message_id=result.get("message_id"),
                              error=result.get("error"))

        async def _unsupported_media(self, *args, **kwargs) -> SendResult:
            return SendResult(success=False, error="Hermex attachments must accompany the standalone text delivery")

        send_document = _unsupported_media
        send_image_file = _unsupported_media
        send_voice = _unsupported_media
        send_video = _unsupported_media

        async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
            return {"name": "Hermex", "type": "dm"}

    return HermexAdapter(config)


PLATFORM_HINT = (
    "Your final reply may be shown as a phone notification preview. Lead with the outcome in one "
    "or two plain sentences before any detail."
)
