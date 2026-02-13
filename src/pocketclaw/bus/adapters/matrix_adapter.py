"""Matrix Channel Adapter — matrix-nio.

Uses AsyncClient.sync_forever() for long-polling.
Streaming via message editing (m.replace relation), rate-limited 1.5s.

Requires: pip install matrix-nio

Created: 2026-02-07
"""

import asyncio
import logging
import time

from pocketclaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

# Rate limit for message edits (streaming)
_EDIT_RATE_LIMIT = 1.5


class MatrixAdapter(BaseChannelAdapter):
    """Adapter for Matrix via matrix-nio."""

    def __init__(
        self,
        homeserver: str = "",
        user_id: str = "",
        access_token: str | None = None,
        password: str | None = None,
        allowed_room_ids: list[str] | None = None,
        device_id: str = "POCKETPAW",
    ):
        super().__init__()
        self.homeserver = homeserver
        self.user_id = user_id
        self.access_token = access_token
        self.password = password
        self.allowed_room_ids = allowed_room_ids or []
        self.device_id = device_id
        self._client = None  # nio.AsyncClient
        self._sync_task: asyncio.Task | None = None
        self._buffers: dict[str, str] = {}
        self._edit_event_ids: dict[str, str] = {}  # chat_id -> event_id for edits
        self._last_edit_time: dict[str, float] = {}

    @property
    def channel(self) -> Channel:
        return Channel.MATRIX

    async def _on_start(self) -> None:
        if not self.homeserver or not self.user_id:
            logger.error("Matrix homeserver and user_id are required")
            return

        try:
            from nio import AsyncClient, RoomMessageText
        except ImportError:
            from pocketclaw.bus.adapters import auto_install

            auto_install("matrix", "nio")
            from nio import AsyncClient, RoomMessageText

        self._client = AsyncClient(
            self.homeserver,
            self.user_id,
            device_id=self.device_id,
        )

        if self.access_token:
            self._client.access_token = self.access_token
            self._client.user_id = self.user_id
            self._client.device_id = self.device_id
        elif self.password:
            resp = await self._client.login(self.password, device_name="PocketPaw")
            if hasattr(resp, "access_token"):
                logger.info("Matrix login successful")
            else:
                logger.error("Matrix login failed: %s", resp)
                return

        # Register message callbacks — text and media types
        self._client.add_event_callback(self._on_message, RoomMessageText)

        try:
            from nio import RoomMessageAudio, RoomMessageFile, RoomMessageImage, RoomMessageVideo

            for evt_type in (RoomMessageImage, RoomMessageFile, RoomMessageAudio, RoomMessageVideo):
                self._client.add_event_callback(self._on_media_message, evt_type)
        except ImportError:
            logger.debug("Matrix media event types not available in nio")

        # Start sync loop
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("Matrix Adapter started (%s)", self.homeserver)

    async def _on_stop(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.close()
        logger.info("Matrix Adapter stopped")

    async def _sync_loop(self) -> None:
        """Long-polling sync loop."""
        try:
            # Initial sync to avoid processing old messages
            await self._client.sync(timeout=10000)
            # Now sync forever for new messages
            await self._client.sync_forever(timeout=30000)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Matrix sync error: %s", e)

    async def _on_message(self, room, event) -> None:
        """Handle incoming Matrix messages."""
        # Skip our own messages
        if event.sender == self.user_id:
            return

        # Room filter
        if self.allowed_room_ids and room.room_id not in self.allowed_room_ids:
            logger.debug("Matrix message from unauthorized room: %s", room.room_id)
            return

        content = event.body or ""
        if not content:
            return

        msg = InboundMessage(
            channel=Channel.MATRIX,
            sender_id=event.sender,
            chat_id=room.room_id,
            content=content,
            metadata={
                "event_id": event.event_id,
                "room_name": getattr(room, "display_name", ""),
            },
        )
        await self._publish_inbound(msg)

    async def _on_media_message(self, room, event) -> None:
        """Handle incoming Matrix media messages (image, file, audio, video)."""
        if event.sender == self.user_id:
            return
        if self.allowed_room_ids and room.room_id not in self.allowed_room_ids:
            return

        content = getattr(event, "body", "") or ""
        media_paths: list[str] = []

        # Download media via mxc:// URL
        mxc_url = getattr(event, "url", None)
        if mxc_url and mxc_url.startswith("mxc://"):
            try:
                from pocketclaw.bus.media import build_media_hint, get_media_downloader

                # Convert mxc://server/media_id to HTTPS download URL
                parts = mxc_url[len("mxc://") :]
                server, _, media_id = parts.partition("/")
                hs = self.homeserver.rstrip("/")
                download_url = f"{hs}/_matrix/media/v3/download/{server}/{media_id}"

                name = getattr(event, "body", "media") or "media"
                mime = None
                info = getattr(event, "source", {}).get("content", {}).get("info", {})
                if info:
                    mime = info.get("mimetype")

                downloader = get_media_downloader()
                path = await downloader.download_url(download_url, name=name, mime=mime)
                media_paths.append(path)
                content += build_media_hint([name])
            except Exception as e:
                logger.warning("Failed to download Matrix media: %s", e)

        if not content and not media_paths:
            return

        msg = InboundMessage(
            channel=Channel.MATRIX,
            sender_id=event.sender,
            chat_id=room.room_id,
            content=content,
            media=media_paths,
            metadata={
                "event_id": event.event_id,
                "room_name": getattr(room, "display_name", ""),
            },
        )
        await self._publish_inbound(msg)

    async def send(self, message: OutboundMessage) -> None:
        """Send message to Matrix.

        Streaming via m.replace editing with rate limiting.
        """
        if not self._client:
            return

        try:
            if message.is_stream_chunk:
                chat_id = message.chat_id
                if chat_id not in self._buffers:
                    self._buffers[chat_id] = ""
                self._buffers[chat_id] += message.content

                # Rate-limited edit-in-place
                now = time.time()
                last = self._last_edit_time.get(chat_id, 0)
                if now - last >= _EDIT_RATE_LIMIT:
                    event_id = self._edit_event_ids.get(chat_id)
                    if event_id:
                        await self._edit_message(chat_id, event_id, self._buffers[chat_id])
                    else:
                        event_id = await self._send_text(chat_id, self._buffers[chat_id])
                        if event_id:
                            self._edit_event_ids[chat_id] = event_id
                    self._last_edit_time[chat_id] = now
                return

            if message.is_stream_end:
                chat_id = message.chat_id
                text = self._buffers.pop(chat_id, "")
                event_id = self._edit_event_ids.pop(chat_id, None)
                self._last_edit_time.pop(chat_id, None)
                if text.strip():
                    if event_id:
                        await self._edit_message(chat_id, event_id, text)
                    else:
                        await self._send_text(chat_id, text)
                return

            if message.content.strip():
                await self._send_text(message.chat_id, message.content)

        except Exception as e:
            logger.error("Failed to send Matrix message: %s", e)

    async def _send_text(self, room_id: str, text: str) -> str | None:
        """Send a text message and return the event_id."""
        if not self._client:
            return None
        try:
            from nio import RoomSendResponse

            resp = await self._client.room_send(
                room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": text},
            )
            if isinstance(resp, RoomSendResponse):
                return resp.event_id
            logger.error("Matrix send error: %s", resp)
        except Exception as e:
            logger.error("Matrix send error: %s", e)
        return None

    async def _edit_message(self, room_id: str, event_id: str, new_text: str) -> None:
        """Edit an existing message using m.replace."""
        if not self._client:
            return
        try:
            await self._client.room_send(
                room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": f"* {new_text}",
                    "m.new_content": {"msgtype": "m.text", "body": new_text},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
                },
            )
        except Exception as e:
            logger.error("Matrix edit error: %s", e)
