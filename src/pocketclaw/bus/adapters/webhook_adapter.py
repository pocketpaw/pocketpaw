"""Generic inbound webhook adapter.

Allows arbitrary external services (GitHub, Zapier, n8n, Home Assistant, cron
scripts, etc.) to push events into PocketPaw via HTTP POST.

Each webhook gets its own URL and secret. Supports both async (fire-and-forget)
and sync (wait-for-response) modes.

Created: 2026-02-09
"""

import asyncio
import json
import logging
from dataclasses import dataclass

from pocketclaw.bus.adapters import BaseChannelAdapter
from pocketclaw.bus.events import Channel, InboundMessage, OutboundMessage

_log = logging.getLogger(__name__)


@dataclass
class WebhookSlotConfig:
    """Configuration for a single webhook slot."""

    name: str
    secret: str
    description: str = ""
    sync_timeout: int = 30


class WebhookAdapter(BaseChannelAdapter):
    """Adapter for generic inbound webhooks.

    Unlike other adapters, WebhookAdapter does not poll or maintain a persistent
    connection. Instead, it is driven by the dashboard's HTTP endpoint which
    calls ``handle_webhook()`` for each incoming request.

    In sync mode, the adapter registers an ``asyncio.Future`` keyed by a request
    UUID. When the agent produces an ``OutboundMessage`` targeting that UUID, the
    future resolves and the HTTP handler returns the response to the caller.
    """

    def __init__(self) -> None:
        super().__init__()
        # Pending sync futures: request_id -> Future[str]
        self._pending: dict[str, asyncio.Future[str]] = {}
        # Stream buffers for sync mode: request_id -> accumulated chunks
        self._buffers: dict[str, list[str]] = {}

    @property
    def channel(self) -> Channel:
        return Channel.WEBHOOK

    async def send(self, message: OutboundMessage) -> None:
        """Capture outbound messages for sync-mode callers."""
        request_id = message.chat_id

        if request_id not in self._pending:
            # Async mode — no one is waiting, just log
            _log.debug("Webhook outbound (async, no waiter): %s", request_id)
            return

        if message.is_stream_chunk and not message.is_stream_end:
            # Accumulate streaming chunks
            self._buffers.setdefault(request_id, []).append(message.content)
            return

        if message.is_stream_end:
            # Resolve with accumulated content
            chunks = self._buffers.pop(request_id, [])
            full_response = "".join(chunks)
            fut = self._pending.pop(request_id, None)
            if fut and not fut.done():
                fut.set_result(full_response)
            return

        # Non-streaming final message
        fut = self._pending.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(message.content)

    async def handle_webhook(
        self,
        slot: WebhookSlotConfig,
        body: dict,
        request_id: str,
        sync: bool = False,
    ) -> str | None:
        """Process an incoming webhook payload.

        Args:
            slot: The webhook slot configuration.
            body: The parsed JSON body.
            request_id: Unique identifier for this request.
            sync: If True, wait for the agent's response.

        Returns:
            The agent's response text if sync=True, else None.
        """
        # Parse payload — standard format or raw fallback
        content = body.get("content")
        if content is None:
            # Raw fallback: entire body becomes content
            content = json.dumps(body)

        sender = body.get("sender", f"webhook:{slot.name}")
        extra_metadata = body.get("metadata", {})
        if not isinstance(extra_metadata, dict):
            extra_metadata = {}

        # Download media URLs if provided
        media_paths: list[str] = []
        media_urls = body.get("media_urls", [])
        if media_urls and isinstance(media_urls, list):
            try:
                from pocketclaw.bus.media import build_media_hint, get_media_downloader

                downloader = get_media_downloader()
                names = []
                for url in media_urls:
                    if not isinstance(url, str):
                        continue
                    try:
                        path = await downloader.download_url(url)
                        media_paths.append(path)
                        # Extract filename from path
                        names.append(path.rsplit("/", 1)[-1])
                    except Exception as e:
                        _log.warning("Failed to download webhook media: %s", e)
                if names:
                    content += build_media_hint(names)
            except Exception as e:
                _log.warning("Webhook media download error: %s", e)

        metadata = {
            "webhook_name": slot.name,
            "source": "webhook",
            **extra_metadata,
        }

        msg = InboundMessage(
            channel=Channel.WEBHOOK,
            sender_id=str(sender),
            chat_id=request_id,
            content=str(content),
            media=media_paths,
            metadata=metadata,
        )

        if sync:
            # Register a future before publishing
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            self._pending[request_id] = fut
            self._buffers.pop(request_id, None)

        await self._publish_inbound(msg)

        if not sync:
            return None

        # Wait for the response with timeout
        timeout = slot.sync_timeout
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except TimeoutError:
            self._pending.pop(request_id, None)
            self._buffers.pop(request_id, None)
            _log.warning("Sync webhook timed out after %ds: %s", timeout, request_id)
            return None
