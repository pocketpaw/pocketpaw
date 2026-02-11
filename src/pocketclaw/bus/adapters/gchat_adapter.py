"""Google Chat Channel Adapter.

Supports two modes:
- webhook: POST to /webhook/gchat (mounted on dashboard or standalone)
- pubsub: Google Cloud Pub/Sub polling

Requires: pip install google-api-python-client google-auth

Created: 2026-02-07
"""

import asyncio
import logging

from pocketclaw.bus import BaseChannelAdapter, Channel, InboundMessage, OutboundMessage
from pocketclaw.bus.format import convert_markdown

logger = logging.getLogger(__name__)


class GoogleChatAdapter(BaseChannelAdapter):
    """Adapter for Google Chat (Workspace)."""

    def __init__(
        self,
        mode: str = "webhook",
        service_account_key: str | None = None,
        project_id: str | None = None,
        subscription_id: str | None = None,
        allowed_space_ids: list[str] | None = None,
    ):
        super().__init__()
        self.mode = mode
        self.service_account_key = service_account_key
        self.project_id = project_id
        self.subscription_id = subscription_id
        self.allowed_space_ids = allowed_space_ids or []
        self._credentials = None
        self._chat_service = None
        self._poll_task: asyncio.Task | None = None
        self._buffers: dict[str, str] = {}

    @property
    def channel(self) -> Channel:
        return Channel.GOOGLE_CHAT

    async def _on_start(self) -> None:
        if self.service_account_key:
            try:
                await self._init_credentials()
            except Exception as e:
                logger.error("Failed to init Google Chat credentials: %s", e)
                return

        if self.mode == "pubsub" and self.project_id and self.subscription_id:
            self._poll_task = asyncio.create_task(self._pubsub_loop())

        logger.info("Google Chat Adapter started (mode=%s)", self.mode)

    async def _on_stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("Google Chat Adapter stopped")

    async def _init_credentials(self) -> None:
        """Initialize Google service account credentials."""
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            from pocketclaw.bus.adapters import auto_install

            auto_install("gchat", "googleapiclient")
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/chat.bot"]
        self._credentials = service_account.Credentials.from_service_account_file(
            self.service_account_key,
            scopes=scopes,
        )
        self._chat_service = build("chat", "v1", credentials=self._credentials)

    async def handle_webhook_message(self, payload: dict) -> None:
        """Handle incoming Google Chat webhook payload."""
        try:
            event_type = payload.get("type", "")
            if event_type != "MESSAGE":
                return

            message = payload.get("message", {})
            sender = message.get("sender", {})
            sender_name = sender.get("name", "")
            sender_display = sender.get("displayName", "")
            text = message.get("text", "") or message.get("argumentText", "")
            space = payload.get("space", {})
            space_name = space.get("name", "")

            # Space filter
            if self.allowed_space_ids and space_name not in self.allowed_space_ids:
                logger.debug("Google Chat message from unauthorized space: %s", space_name)
                return

            # Download attachments
            media_paths: list[str] = []
            attachment = message.get("attachment")
            if attachment:
                attachments = attachment if isinstance(attachment, list) else [attachment]
                try:
                    from pocketclaw.bus.media import build_media_hint, get_media_downloader

                    downloader = get_media_downloader()
                    names = []
                    for att in attachments:
                        download_uri = att.get("downloadUri") or att.get(
                            "attachmentDataRef", {}
                        ).get("resourceName")
                        if not download_uri:
                            continue
                        name = att.get("name", "attachment") or "attachment"
                        mime = att.get("contentType")
                        try:
                            path = await downloader.download_url(download_uri, name=name, mime=mime)
                            media_paths.append(path)
                            names.append(name)
                        except Exception as e:
                            logger.warning("Failed to download Google Chat attachment: %s", e)
                    if names:
                        text += build_media_hint(names)
                except Exception as e:
                    logger.warning("Google Chat media download error: %s", e)

            if not text and not media_paths:
                return

            msg = InboundMessage(
                channel=Channel.GOOGLE_CHAT,
                sender_id=sender_name,
                chat_id=space_name,
                content=text,
                media=media_paths,
                metadata={
                    "message_name": message.get("name", ""),
                    "sender_display_name": sender_display,
                    "space_name": space_name,
                    "thread_name": message.get("thread", {}).get("name", ""),
                },
            )
            await self._publish_inbound(msg)

        except Exception as e:
            logger.error("Error processing Google Chat webhook: %s", e)

    async def _pubsub_loop(self) -> None:
        """Poll Google Cloud Pub/Sub for messages."""
        try:
            from google.cloud import pubsub_v1
        except ImportError:
            logger.error("google-cloud-pubsub required for pubsub mode")
            return

        import json

        subscriber = pubsub_v1.SubscriberClient()
        subscription_path = subscriber.subscription_path(self.project_id, self.subscription_id)

        while self._running:
            try:
                response = subscriber.pull(
                    request={"subscription": subscription_path, "max_messages": 10},
                    timeout=30,
                )

                ack_ids = []
                for received_message in response.received_messages:
                    ack_ids.append(received_message.ack_id)
                    try:
                        payload = json.loads(received_message.message.data.decode("utf-8"))
                        await self.handle_webhook_message(payload)
                    except Exception as e:
                        logger.error("Error processing Pub/Sub message: %s", e)

                if ack_ids:
                    subscriber.acknowledge(
                        request={"subscription": subscription_path, "ack_ids": ack_ids}
                    )

            except Exception as e:
                logger.debug("Pub/Sub poll: %s", e)

            await asyncio.sleep(2)

    async def send(self, message: OutboundMessage) -> None:
        """Send message to Google Chat.

        Streaming via message update API with accumulation.
        """
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
            logger.error("Failed to send Google Chat message: %s", e)

    async def _send_text(self, space_name: str, text: str) -> None:
        """Send a text message via Chat API."""
        text = convert_markdown(text, self.channel)
        if not self._chat_service:
            logger.warning("Google Chat service not initialized, cannot send message")
            return

        try:
            self._chat_service.spaces().messages().create(
                parent=space_name,
                body={"text": text},
            ).execute()
        except Exception as e:
            logger.error("Google Chat API error: %s", e)
