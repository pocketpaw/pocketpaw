"""
Slack Channel Adapter (Socket Mode).
Created: 2026-02-06
"""

import asyncio
import logging
from typing import Any

from pocketclaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage
from pocketclaw.bus.format import convert_markdown

logger = logging.getLogger(__name__)


class SlackAdapter(BaseChannelAdapter):
    """Adapter for Slack using Socket Mode (no public URL needed)."""

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed_channel_ids: list[str] | None = None,
    ):
        super().__init__()
        self.bot_token = bot_token
        self.app_token = app_token
        self.allowed_channel_ids = allowed_channel_ids or []
        self._slack_app: Any = None
        self._handler: Any = None
        self._handler_task: asyncio.Task | None = None
        self._buffers: dict[str, dict[str, Any]] = {}

    @property
    def channel(self) -> Channel:
        return Channel.SLACK

    async def _on_start(self) -> None:
        """Initialize and start Slack bot in Socket Mode."""
        if not self.bot_token or not self.app_token:
            raise RuntimeError("Slack bot_token and app_token are both required")

        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError:
            from pocketclaw.bus.adapters import auto_install

            auto_install("slack", "slack_bolt")
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp

        app = AsyncApp(token=self.bot_token)
        adapter = self  # closure reference

        @app.event("app_mention")
        async def handle_mention(event, say):
            await adapter._handle_slack_event(event)

        @app.event("message")
        async def handle_message(event, say):
            # Only handle DMs (channel_type == "im")
            if event.get("channel_type") != "im":
                return
            # Ignore bot messages
            if event.get("bot_id") or event.get("subtype"):
                return
            await adapter._handle_slack_event(event)

        # Register native slash commands so Slack doesn't swallow /cmd
        # messages with "command not found".  These arrive via Socket Mode
        # and are forwarded to the bus as InboundMessage with command text.
        # NOTE: Each command must also be registered in the Slack App
        # manifest at api.slack.com → Slash Commands for this to work.
        for _cmd_name in (
            "/new",
            "/sessions",
            "/resume",
            "/clear",
            "/rename",
            "/status",
            "/delete",
            "/help",
        ):

            @app.command(_cmd_name)
            async def _slash_handler(ack, command, _cmd=_cmd_name):
                await ack()
                text = command.get("text", "").strip()
                content = f"{_cmd} {text}" if text else _cmd
                ch_id = command.get("channel_id", "")
                user = command.get("user_id", "")
                thread_ts = command.get("thread_ts")
                meta: dict[str, Any] = {"channel_id": ch_id}
                if thread_ts:
                    meta["thread_ts"] = thread_ts
                msg = InboundMessage(
                    channel=Channel.SLACK,
                    sender_id=user,
                    chat_id=ch_id,
                    content=content,
                    metadata=meta,
                )
                await adapter._publish_inbound(msg)

        self._slack_app = app
        self._handler = AsyncSocketModeHandler(app, self.app_token)
        self._handler_task = asyncio.create_task(self._handler.start_async())
        logger.info("Slack Adapter started (Socket Mode)")

    async def _on_stop(self) -> None:
        """Stop Slack bot."""
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception as e:
                logger.warning(f"Error stopping Slack handler: {e}")
        if self._handler_task and not self._handler_task.done():
            self._handler_task.cancel()
            try:
                await self._handler_task
            except asyncio.CancelledError:
                pass
        logger.info("Slack Adapter stopped")

    async def _handle_slack_event(self, event: dict) -> None:
        """Process an incoming Slack event and publish to bus."""
        channel_id = event.get("channel", "")
        user_id = event.get("user", "")
        text = event.get("text", "")

        if not user_id:
            return

        # Channel filter
        if self.allowed_channel_ids and channel_id not in self.allowed_channel_ids:
            return

        # Strip bot mention from text (e.g. <@U12345> hello -> hello)
        import re

        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

        # Download attached files
        media_paths: list[str] = []
        files = event.get("files", [])
        if files:
            try:
                from pocketclaw.bus.media import build_media_hint, get_media_downloader

                downloader = get_media_downloader()
                names = []
                for f in files:
                    url = f.get("url_private_download") or f.get("url_private")
                    if not url:
                        continue
                    name = f.get("name", "file")
                    mime = f.get("mimetype")
                    try:
                        path = await downloader.download_url_with_auth(
                            url, f"Bearer {self.bot_token}", name=name, mime=mime
                        )
                        media_paths.append(path)
                        names.append(name)
                    except Exception as e:
                        logger.warning("Failed to download Slack file: %s", e)
                if names:
                    text += build_media_hint(names)
            except Exception as e:
                logger.warning("Slack media download error: %s", e)

        if not text and not media_paths:
            return

        metadata: dict[str, Any] = {
            "channel_id": channel_id,
        }
        # Pass thread_ts for threaded replies
        thread_ts = event.get("thread_ts") or event.get("ts")
        if thread_ts:
            metadata["thread_ts"] = thread_ts

        msg = InboundMessage(
            channel=Channel.SLACK,
            sender_id=user_id,
            chat_id=channel_id,
            content=text,
            media=media_paths,
            metadata=metadata,
        )
        await self._publish_inbound(msg)

    async def send(self, message: OutboundMessage) -> None:
        """Send message to Slack channel."""
        if not self._slack_app:
            return

        try:
            if message.is_stream_chunk:
                await self._handle_stream_chunk(message)
                return

            if message.is_stream_end:
                await self._flush_stream_buffer(message.chat_id)
                return

            # Normal message
            kwargs: dict[str, Any] = {
                "channel": message.chat_id,
                "text": convert_markdown(message.content, self.channel),
            }
            thread_ts = message.metadata.get("thread_ts")
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            await self._slack_app.client.chat_postMessage(**kwargs)

        except Exception as e:
            logger.error(f"Failed to send Slack message: {e}")

    # --- Stream buffering ---

    async def _handle_stream_chunk(self, message: OutboundMessage) -> None:
        chat_id = message.chat_id
        content = message.content
        thread_ts = message.metadata.get("thread_ts")

        if chat_id not in self._buffers:
            kwargs: dict[str, Any] = {
                "channel": chat_id,
                "text": "...",
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            result = await self._slack_app.client.chat_postMessage(**kwargs)
            self._buffers[chat_id] = {
                "ts": result["ts"],
                "text": content,
                "thread_ts": thread_ts,
                "last_update": asyncio.get_event_loop().time(),
            }
        else:
            self._buffers[chat_id]["text"] += content

        now = asyncio.get_event_loop().time()
        buf = self._buffers[chat_id]
        if now - buf["last_update"] > 1.5:
            await self._update_buffer_message(chat_id)
            buf["last_update"] = now

    async def _flush_stream_buffer(self, chat_id: str) -> None:
        if chat_id in self._buffers:
            await self._update_buffer_message(chat_id)
            del self._buffers[chat_id]

    async def _update_buffer_message(self, chat_id: str) -> None:
        buf = self._buffers.get(chat_id)
        if not buf:
            return
        text = convert_markdown(buf["text"], self.channel)
        if not text.strip():
            return
        try:
            kwargs: dict[str, Any] = {
                "channel": chat_id,
                "ts": buf["ts"],
                "text": text,
            }
            await self._slack_app.client.chat_update(**kwargs)
        except Exception as e:
            logger.warning(f"Failed to update Slack message: {e}")
