"""
WhatsApp Channel Adapter (Business Cloud API).
Created: 2026-02-06
"""

import logging

import httpx

from pocketclaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage
from pocketclaw.bus.format import convert_markdown

logger = logging.getLogger(__name__)

WHATSAPP_API_VERSION = "v21.0"
WHATSAPP_API_BASE = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"


class WhatsAppAdapter(BaseChannelAdapter):
    """Adapter for WhatsApp Business Cloud API."""

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        verify_token: str,
        allowed_phone_numbers: list[str] | None = None,
    ):
        super().__init__()
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.verify_token = verify_token
        self.allowed_phone_numbers = allowed_phone_numbers or []
        self._http: httpx.AsyncClient | None = None
        self._buffers: dict[str, str] = {}

    @property
    def channel(self) -> Channel:
        return Channel.WHATSAPP

    async def _on_start(self) -> None:
        """Initialize HTTP client."""
        if not self.access_token or not self.phone_number_id:
            logger.error("WhatsApp access_token and phone_number_id are required")
            return

        self._http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        logger.info("WhatsApp Adapter started")

    async def _on_stop(self) -> None:
        """Close HTTP client."""
        if self._http:
            await self._http.aclose()
        logger.info("WhatsApp Adapter stopped")

    def handle_webhook_verify(
        self, mode: str | None, token: str | None, challenge: str | None
    ) -> str | None:
        """Handle Meta webhook verification challenge.

        Returns the challenge string on success, None on failure.
        """
        if mode == "subscribe" and token == self.verify_token:
            return challenge
        return None

    async def handle_webhook_message(self, payload: dict) -> None:
        """Parse incoming WhatsApp webhook payload and publish to bus."""
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    messages = value.get("messages", [])

                    for msg_data in messages:
                        sender = msg_data.get("from", "")

                        # Phone number filter
                        if self.allowed_phone_numbers and sender not in self.allowed_phone_numbers:
                            logger.debug(f"WhatsApp message from unauthorized number: {sender}")
                            continue

                        content, media_paths = await self._extract_content_and_media(msg_data)
                        if not content and not media_paths:
                            continue

                        msg = InboundMessage(
                            channel=Channel.WHATSAPP,
                            sender_id=sender,
                            chat_id=sender,
                            content=content,
                            media=media_paths,
                            metadata={
                                "message_id": msg_data.get("id", ""),
                                "message_type": msg_data.get("type", "text"),
                            },
                        )
                        await self._publish_inbound(msg)

                        # Send read receipt
                        msg_id = msg_data.get("id")
                        if msg_id:
                            await self._mark_as_read(msg_id)

        except Exception as e:
            logger.error(f"Error processing WhatsApp webhook: {e}")

    async def _extract_content_and_media(self, msg_data: dict) -> tuple[str, list[str]]:
        """Extract text content and download media from a WhatsApp message.

        Returns (content_text, list_of_local_media_paths).
        """
        msg_type = msg_data.get("type", "text")
        media_paths: list[str] = []

        if msg_type == "text":
            return msg_data.get("text", {}).get("body", ""), []

        # Media types that can carry a caption and media_id
        media_key_map = {
            "image": "image",
            "document": "document",
            "audio": "audio",
            "video": "video",
            "sticker": "sticker",
        }
        media_key = media_key_map.get(msg_type)
        if media_key:
            media_block = msg_data.get(media_key, {})
            caption = media_block.get("caption", "")
            media_id = media_block.get("id")
            mime = media_block.get("mime_type")
            filename = media_block.get("filename") or f"{msg_type}"

            if media_id:
                try:
                    path = await self._download_whatsapp_media(media_id, filename, mime)
                    if path:
                        media_paths.append(path)
                        from pocketclaw.bus.media import build_media_hint

                        caption += build_media_hint([filename])
                except Exception as e:
                    logger.warning("Failed to download WhatsApp media: %s", e)

            return caption or f"[{msg_type} received]", media_paths

        return f"[{msg_type} message received]", []

    async def _download_whatsapp_media(
        self, media_id: str, name: str, mime: str | None
    ) -> str | None:
        """Two-step WhatsApp media download: get URL from media_id, then download."""
        if not self._http:
            return None

        # Step 1: Get the media URL
        url_resp = await self._http.get(f"{WHATSAPP_API_BASE}/{media_id}")
        if url_resp.status_code != 200:
            logger.warning("WhatsApp media URL fetch failed: %s", url_resp.text)
            return None

        media_url = url_resp.json().get("url")
        if not media_url:
            return None

        # Step 2: Download the actual file (reuse existing auth headers)
        from pocketclaw.bus.media import get_media_downloader

        downloader = get_media_downloader()
        return await downloader.download_url_with_auth(
            media_url, f"Bearer {self.access_token}", name=name, mime=mime
        )

    async def _mark_as_read(self, message_id: str) -> None:
        """Send read receipt for a message."""
        if not self._http:
            return
        try:
            url = f"{WHATSAPP_API_BASE}/{self.phone_number_id}/messages"
            await self._http.post(
                url,
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                },
            )
        except Exception as e:
            logger.debug(f"Failed to send read receipt: {e}")

    async def send(self, message: OutboundMessage) -> None:
        """Send message to WhatsApp.

        WhatsApp doesn't support streaming — accumulate chunks and send on stream_end.
        """
        if not self._http:
            return

        try:
            if message.is_stream_chunk:
                # Accumulate in buffer
                chat_id = message.chat_id
                if chat_id not in self._buffers:
                    self._buffers[chat_id] = ""
                self._buffers[chat_id] += message.content
                return

            if message.is_stream_end:
                # Flush accumulated buffer
                chat_id = message.chat_id
                text = self._buffers.pop(chat_id, "")
                if text.strip():
                    await self._send_text(chat_id, text)
                return

            # Normal message
            if message.content.strip():
                await self._send_text(message.chat_id, message.content)

        except Exception as e:
            logger.error(f"Failed to send WhatsApp message: {e}")

    async def _send_text(self, to: str, text: str) -> None:
        """Send a text message via the WhatsApp Cloud API."""
        if not self._http:
            return
        text = convert_markdown(text, self.channel)
        url = f"{WHATSAPP_API_BASE}/{self.phone_number_id}/messages"
        resp = await self._http.post(
            url,
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )
        if resp.status_code >= 400:
            logger.error(f"WhatsApp API error ({resp.status_code}): {resp.text}")
