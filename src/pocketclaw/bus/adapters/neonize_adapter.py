"""
WhatsApp Channel Adapter — Personal Mode (neonize / WhatsApp Web multi-device).

Uses neonize (Python wrapper around whatsmeow) for QR-code-scan pairing.
No Meta Developer account, webhooks, or tunnels required.

Created: 2026-02-06
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from pocketclaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage
from pocketclaw.bus.format import convert_markdown

logger = logging.getLogger(__name__)

# Module-level lock — neonize's event_global_loop must be started exactly once
_neonize_loop_started = False
_neonize_loop_lock = threading.Lock()


def _ensure_neonize_loop_running() -> None:
    """Start neonize's internal event loop in a daemon thread if not already running.

    Neonize creates its own asyncio loop (event_global_loop) for dispatching
    QR callbacks, message events, etc. via run_coroutine_threadsafe().  That loop
    must be running for any events to fire.  It is NOT started by default.
    """
    global _neonize_loop_started
    if _neonize_loop_started:
        return
    with _neonize_loop_lock:
        if _neonize_loop_started:
            return
        from neonize.aioze.events import event_global_loop

        thread = threading.Thread(
            target=event_global_loop.run_forever,
            daemon=True,
            name="neonize-event-loop",
        )
        thread.start()
        _neonize_loop_started = True
        logger.debug("neonize event_global_loop started in background thread")


class NeonizeAdapter(BaseChannelAdapter):
    """WhatsApp adapter using neonize (WhatsApp Web multi-device protocol)."""

    def __init__(self, db_path: str | None = None):
        super().__init__()
        self._client: Any = None
        self._db_path = db_path or str(Path.home() / ".pocketclaw" / "neonize.sqlite3")
        self._qr_data: str | None = None  # latest QR string for REST endpoint
        self._connected = False
        self._client_task: asyncio.Task | None = None
        self._connect_future: Any = None
        self._neonize_loop: Any = None  # neonize's event_global_loop (set in _on_start)
        self._buffers: dict[str, str] = {}
        self._jid_cache: dict[str, Any] = {}  # chat_id string → JID protobuf

    @property
    def channel(self) -> Channel:
        return Channel.WHATSAPP

    async def _on_start(self) -> None:
        """Initialize and start neonize WhatsApp client."""
        try:
            from neonize.aioze.client import NewAClient
            from neonize.aioze.events import ConnectedEv, MessageEv
            from neonize.utils.jid import Jid2String
        except ImportError:
            from pocketclaw.bus.adapters import auto_install

            auto_install("whatsapp-personal", "neonize")
            from neonize.aioze.client import NewAClient
            from neonize.aioze.events import ConnectedEv, MessageEv
            from neonize.utils.jid import Jid2String

        # Neonize dispatches events via its own loop — make sure it's running
        _ensure_neonize_loop_running()

        # Ensure parent directory exists
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        client = NewAClient(self._db_path)
        adapter = self  # closure reference

        @client.qr
        async def on_qr(client: Any, qr_data: bytes):
            # qr_data is the raw string to encode into a QR code
            qr_str = qr_data.decode("utf-8") if isinstance(qr_data, bytes) else str(qr_data)
            adapter._qr_data = qr_str
            logger.info("WhatsApp QR code received — scan with your phone")

        @client.event(ConnectedEv)
        async def on_connected(client: Any, event: Any):
            adapter._connected = True
            adapter._qr_data = None  # clear QR once paired
            logger.info("WhatsApp (neonize) connected successfully")

        @client.event(MessageEv)
        async def on_message(client: Any, event: Any):
            try:
                info = event.Info
                source = info.MessageSource

                # Skip messages sent by the paired account itself
                if source.IsFromMe:
                    return

                sender_jid = source.Sender
                chat_jid = source.Chat

                # Convert protobuf JID → clean string (e.g. "123456@s.whatsapp.net")
                sender = Jid2String(sender_jid)
                chat_id = Jid2String(chat_jid)

                # Cache JID objects so we can reuse them for replies
                adapter._jid_cache[sender] = sender_jid
                adapter._jid_cache[chat_id] = chat_jid

                # Extract text content
                message = event.Message
                content = message.conversation or (
                    message.extendedTextMessage.text if message.extendedTextMessage else ""
                )

                # Download media if present
                media_paths: list[str] = []
                media_msg = (
                    message.imageMessage
                    or message.documentMessage
                    or message.audioMessage
                    or message.videoMessage
                    or message.stickerMessage
                )
                if media_msg:
                    try:
                        from pocketclaw.bus.media import build_media_hint, get_media_downloader

                        data = await client.download_any(message)
                        mime = getattr(media_msg, "mimetype", None)
                        fname = getattr(media_msg, "fileName", None) or "media"
                        # Use caption if available
                        caption = getattr(media_msg, "caption", "")
                        if caption and not content:
                            content = caption

                        downloader = get_media_downloader()
                        path = await downloader.save_from_bytes(bytes(data), fname, mime)
                        media_paths.append(path)
                        content += build_media_hint([fname])
                    except Exception as e:
                        logger.warning("Failed to download neonize media: %s", e)

                if not content and not media_paths:
                    return

                msg = InboundMessage(
                    channel=Channel.WHATSAPP,
                    sender_id=sender,
                    chat_id=chat_id,
                    content=content,
                    media=media_paths,
                    metadata={"source": "neonize"},
                )
                await adapter._publish_inbound(msg)
            except Exception as e:
                logger.error(f"Error processing neonize message: {e}")

        self._client = client
        self._jid2string = Jid2String

        # Schedule connect() on neonize's own event loop (not FastAPI's)
        from neonize.aioze.events import event_global_loop

        self._neonize_loop = event_global_loop
        future = asyncio.run_coroutine_threadsafe(client.connect(), event_global_loop)
        self._connect_future = future

        # Give the client a moment to initialize and produce a QR code
        await asyncio.sleep(3)
        logger.info("WhatsApp (neonize) Adapter started — scan QR code to pair")

    async def _on_stop(self) -> None:
        """Stop neonize client."""
        if self._client:
            try:
                if self._neonize_loop and self._neonize_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._client.disconnect(), self._neonize_loop
                    )
                    future.result(timeout=5)
                else:
                    await self._client.disconnect()
            except Exception as e:
                logger.debug(f"Neonize disconnect: {e}")

        if self._connect_future:
            self._connect_future.cancel()

        if self._client_task and not self._client_task.done():
            self._client_task.cancel()
            try:
                await self._client_task
            except asyncio.CancelledError:
                pass

        self._connected = False
        self._qr_data = None
        logger.info("WhatsApp (neonize) Adapter stopped")

    async def send(self, message: OutboundMessage) -> None:
        """Send message to WhatsApp via neonize.

        WhatsApp doesn't support streaming — accumulate chunks, send on stream_end.
        """
        if not self._client or not self._connected:
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
                if text.strip():
                    await self._send_text(chat_id, text)
                return

            # Normal message
            if message.content.strip():
                await self._send_text(message.chat_id, message.content)

        except Exception as e:
            logger.error(f"Failed to send neonize message: {e}")

    async def _send_text(self, to: str, text: str) -> None:
        """Send a text message via neonize."""
        if not self._client:
            return
        text = convert_markdown(text, self.channel)
        try:
            # Look up the cached JID protobuf; fall back to building from string
            jid = self._jid_cache.get(to)
            if jid is None:
                from neonize.utils.jid import build_jid

                # Parse "user@server" format if present
                if "@" in to:
                    user, server = to.split("@", 1)
                    jid = build_jid(user, server)
                else:
                    jid = build_jid(to)

            if self._neonize_loop and self._neonize_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    self._client.send_message(jid, text), self._neonize_loop
                )
                future.result(timeout=30)
            else:
                await self._client.send_message(jid, text)
        except Exception as e:
            logger.error(f"Neonize send error: {e}")
