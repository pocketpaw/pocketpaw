"""Signal Channel Adapter — signal-cli REST API.

Polls GET /v1/receive/{number} every 2 seconds for incoming messages.
Sends via POST /v2/send. No streaming — accumulate chunks and send on stream_end.

Requires a running signal-cli-rest-api instance:
  https://github.com/bbernhard/signal-cli-rest-api

Created: 2026-02-07
"""

import asyncio
import logging

import httpx

from pocketclaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage
from pocketclaw.bus.format import convert_markdown

logger = logging.getLogger(__name__)


class SignalAdapter(BaseChannelAdapter):
    """Adapter for Signal via signal-cli REST API."""

    def __init__(
        self,
        api_url: str = "http://localhost:8080",
        phone_number: str = "",
        allowed_phone_numbers: list[str] | None = None,
    ):
        super().__init__()
        self.api_url = api_url.rstrip("/")
        self.phone_number = phone_number
        self.allowed_phone_numbers = allowed_phone_numbers or []
        self._http: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._buffers: dict[str, str] = {}

    @property
    def channel(self) -> Channel:
        return Channel.SIGNAL

    async def _on_start(self) -> None:
        if not self.phone_number:
            logger.error("Signal phone_number is required")
            return
        self._http = httpx.AsyncClient(timeout=30.0)
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Signal Adapter started (polling %s)", self.api_url)

    async def _on_stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._http:
            await self._http.aclose()
        logger.info("Signal Adapter stopped")

    async def _poll_loop(self) -> None:
        """Poll signal-cli REST API for new messages."""
        url = f"{self.api_url}/v1/receive/{self.phone_number}"
        while self._running:
            try:
                resp = await self._http.get(url)
                if resp.status_code == 200:
                    messages = resp.json()
                    for msg_data in messages:
                        await self._handle_message(msg_data)
            except httpx.HTTPError as e:
                logger.debug("Signal poll error: %s", e)
            except Exception as e:
                logger.error("Signal poll error: %s", e)
            await asyncio.sleep(2)

    async def _handle_message(self, msg_data: dict) -> None:
        """Process a single message from signal-cli."""
        envelope = msg_data.get("envelope", {})
        source = envelope.get("source", "") or envelope.get("sourceNumber", "")
        data_msg = envelope.get("dataMessage", {})
        content = data_msg.get("message", "")

        if not source:
            return

        # Filter by allowed phone numbers
        if self.allowed_phone_numbers and source not in self.allowed_phone_numbers:
            logger.debug("Signal message from unauthorized number: %s", source)
            return

        # Download attachments
        media_paths: list[str] = []
        attachments = data_msg.get("attachments", [])
        if attachments and self._http:
            try:
                from pocketclaw.bus.media import build_media_hint, get_media_downloader

                downloader = get_media_downloader()
                names = []
                for att in attachments:
                    att_id = att.get("id")
                    if not att_id:
                        continue
                    name = att.get("filename") or "attachment"
                    mime = att.get("contentType")
                    try:
                        url = f"{self.api_url}/v1/attachments/{att_id}"
                        path = await downloader.download_url(url, name=name, mime=mime)
                        media_paths.append(path)
                        names.append(name)
                    except Exception as e:
                        logger.warning("Failed to download Signal attachment: %s", e)
                if names:
                    content += build_media_hint(names)
            except Exception as e:
                logger.warning("Signal media download error: %s", e)

        if not content and not media_paths:
            return

        msg = InboundMessage(
            channel=Channel.SIGNAL,
            sender_id=source,
            chat_id=source,
            content=content,
            media=media_paths,
            metadata={
                "timestamp": envelope.get("timestamp", ""),
            },
        )
        await self._publish_inbound(msg)

    async def send(self, message: OutboundMessage) -> None:
        """Send message via Signal.

        No streaming — accumulate chunks and send on stream_end.
        """
        if not self._http:
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

            if message.content.strip():
                await self._send_text(message.chat_id, message.content)

        except Exception as e:
            logger.error("Failed to send Signal message: %s", e)

    async def _send_text(self, to: str, text: str) -> None:
        """Send a text message via signal-cli REST API."""
        if not self._http:
            return
        text = convert_markdown(text, self.channel)
        url = f"{self.api_url}/v2/send"
        resp = await self._http.post(
            url,
            json={
                "message": text,
                "number": self.phone_number,
                "recipients": [to],
            },
        )
        if resp.status_code >= 400:
            logger.error("Signal API error (%d): %s", resp.status_code, resp.text)
