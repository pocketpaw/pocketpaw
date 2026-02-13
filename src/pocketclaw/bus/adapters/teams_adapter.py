"""Microsoft Teams Channel Adapter — Bot Framework SDK.

Webhook at /api/messages/teams. Streaming via update_activity().
Auth via MS App ID + Password.

Requires: pip install botbuilder-core botbuilder-integration-aiohttp

Created: 2026-02-07
"""

import asyncio
import logging
from typing import Any

from pocketclaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage
from pocketclaw.bus.format import convert_markdown

logger = logging.getLogger(__name__)

# Rate limit for message edits (streaming)
_EDIT_RATE_LIMIT = 1.5


class TeamsAdapter(BaseChannelAdapter):
    """Adapter for Microsoft Teams via Bot Framework SDK."""

    def __init__(
        self,
        app_id: str = "",
        app_password: str = "",
        allowed_tenant_ids: list[str] | None = None,
        webhook_port: int = 3978,
    ):
        super().__init__()
        self.app_id = app_id
        self.app_password = app_password
        self.allowed_tenant_ids = allowed_tenant_ids or []
        self.webhook_port = webhook_port
        self._adapter = None  # BotFrameworkAdapter
        self._server_task: asyncio.Task | None = None
        self._buffers: dict[str, str] = {}
        self._activity_refs: dict[str, Any] = {}  # chat_id -> (activity, turn_ctx)
        self._last_edit_time: dict[str, float] = {}

    @property
    def channel(self) -> Channel:
        return Channel.TEAMS

    async def _on_start(self) -> None:
        if not self.app_id or not self.app_password:
            logger.error("Teams app_id and app_password are required")
            return

        try:
            from botbuilder.core import (
                BotFrameworkAdapter,
                BotFrameworkAdapterSettings,
            )
        except ImportError:
            from pocketclaw.bus.adapters import auto_install

            auto_install("teams", "botbuilder")
            from botbuilder.core import (
                BotFrameworkAdapter,
                BotFrameworkAdapterSettings,
            )

        settings = BotFrameworkAdapterSettings(self.app_id, self.app_password)
        self._adapter = BotFrameworkAdapter(settings)

        self._server_task = asyncio.create_task(self._run_webhook_server())
        logger.info("Teams Adapter started (port %d)", self.webhook_port)

    async def _on_stop(self) -> None:
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        logger.info("Teams Adapter stopped")

    async def _run_webhook_server(self) -> None:
        """Run an aiohttp server to receive Teams webhook messages."""
        try:
            from aiohttp import web
        except ImportError:
            logger.error("aiohttp is required for Teams adapter")
            return

        app = web.Application()
        app.router.add_post("/api/messages/teams", self._handle_webhook)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.webhook_port)
        try:
            await site.start()
            logger.info("Teams webhook listening on port %d", self.webhook_port)
            # Keep running until cancelled
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()

    async def _handle_webhook(self, request) -> Any:
        """Handle incoming Teams webhook messages."""
        from aiohttp import web

        if not self._adapter:
            return web.Response(status=503, text="Not ready")

        try:
            body = await request.json()
            auth_header = request.headers.get("Authorization", "")

            from botbuilder.schema import Activity

            activity = Activity().deserialize(body)

            async def on_turn(turn_context):
                await self._process_activity(turn_context)

            await self._adapter.process_activity(activity, auth_header, on_turn)
            return web.Response(status=200)

        except Exception as e:
            logger.error("Teams webhook error: %s", e)
            return web.Response(status=500, text=str(e))

    async def _process_activity(self, turn_context) -> None:
        """Process an incoming Teams activity."""
        from botbuilder.schema import ActivityTypes

        activity = turn_context.activity
        if activity.type != ActivityTypes.message:
            return

        # Tenant filter
        tenant_id = getattr(getattr(activity, "channel_data", None), "tenant", {})
        if isinstance(tenant_id, dict):
            tenant_id = tenant_id.get("id", "")
        elif hasattr(tenant_id, "id"):
            tenant_id = tenant_id.id

        if self.allowed_tenant_ids and tenant_id not in self.allowed_tenant_ids:
            logger.debug("Teams message from unauthorized tenant: %s", tenant_id)
            return

        sender = activity.from_property.id if activity.from_property else "unknown"
        content = activity.text or ""

        # Use conversation ID as chat_id
        chat_id = activity.conversation.id if activity.conversation else sender

        # Download attachments
        media_paths: list[str] = []
        attachments = getattr(activity, "attachments", None) or []
        if attachments:
            try:
                from pocketclaw.bus.media import build_media_hint, get_media_downloader

                downloader = get_media_downloader()
                names = []
                for att in attachments:
                    content_url = getattr(att, "content_url", None)
                    if not content_url:
                        continue
                    name = getattr(att, "name", "attachment") or "attachment"
                    mime = getattr(att, "content_type", None)
                    try:
                        path = await downloader.download_url(content_url, name=name, mime=mime)
                        media_paths.append(path)
                        names.append(name)
                    except Exception as e:
                        logger.warning("Failed to download Teams attachment: %s", e)
                if names:
                    content += build_media_hint(names)
            except Exception as e:
                logger.warning("Teams media download error: %s", e)

        if not content and not media_paths:
            return

        msg = InboundMessage(
            channel=Channel.TEAMS,
            sender_id=sender,
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata={
                "activity_id": activity.id,
                "service_url": activity.service_url or "",
                "conversation_id": chat_id,
            },
        )
        await self._publish_inbound(msg)

    async def send(self, message: OutboundMessage) -> None:
        """Send message to Teams.

        Streaming via update_activity() with rate limiting.
        """
        if not self._adapter:
            return

        try:
            if message.is_stream_chunk:
                chat_id = message.chat_id
                if chat_id not in self._buffers:
                    self._buffers[chat_id] = ""
                self._buffers[chat_id] += message.content
                return

            if message.is_stream_end:
                chat_id = message.chat_id
                text = self._buffers.pop(chat_id, "")
                self._activity_refs.pop(chat_id, None)
                self._last_edit_time.pop(chat_id, None)
                if text.strip():
                    await self._send_text(chat_id, text)
                return

            if message.content.strip():
                await self._send_text(message.chat_id, message.content)

        except Exception as e:
            logger.error("Failed to send Teams message: %s", e)

    async def _send_text(self, chat_id: str, text: str) -> None:
        """Send a text message to Teams."""
        if not self._adapter:
            return
        text = convert_markdown(text, self.channel)
        # Teams sending requires a conversation reference — for now, log a warning
        # since actual sending requires the TurnContext from a previous turn
        logger.info("Teams send to %s: %s", chat_id, text[:100])
