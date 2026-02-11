"""
WebSocket channel adapter.
Created: 2026-02-02
Changes:
  - 2026-02-05: Fixed system_event format - send flat structure for frontend
"""

import logging
from typing import Any

from fastapi import WebSocket

from pocketclaw.bus.adapters import BaseChannelAdapter
from pocketclaw.bus.events import Channel, InboundMessage, OutboundMessage, SystemEvent
from pocketclaw.bus.queue import MessageBus

logger = logging.getLogger(__name__)


class WebSocketAdapter(BaseChannelAdapter):
    """
    WebSocket channel adapter.

    Manages multiple WebSocket connections and routes messages appropriately.
    """

    def __init__(self):
        super().__init__()
        self._connections: dict[str, WebSocket] = {}  # chat_id -> WebSocket

    @property
    def channel(self) -> Channel:
        return Channel.WEBSOCKET

    async def start(self, bus: MessageBus) -> None:
        """Start adapter and subscribe to both outbound and system events."""
        await super().start(bus)
        # Subscribe to system events (thinking, tool usage, etc.)
        bus.subscribe_system(self.on_system_event)
        logger.info("🔌 WebSocket Adapter subscribed to System Events")

    async def on_system_event(self, event: SystemEvent) -> None:
        """Handle system event by broadcasting to all clients."""
        # Send flat structure: {type, event_type, data}
        # Frontend expects event_type and data at top level
        payload = {"type": "system_event", "event_type": event.event_type, "data": event.data}

        # Broadcast to all connected clients
        for ws in self._connections.values():
            try:
                await ws.send_json(payload)
            except Exception:
                pass

    async def register_connection(self, websocket: WebSocket, chat_id: str) -> None:
        """Register a new WebSocket connection."""
        # Assume connection is already accepted by the handler
        self._connections[chat_id] = websocket
        logger.info(f"🔌 WebSocket connected: {chat_id}")

    async def unregister_connection(self, chat_id: str) -> None:
        """Unregister a WebSocket connection."""
        self._connections.pop(chat_id, None)
        logger.info(f"🔌 WebSocket disconnected: {chat_id}")

    async def handle_message(self, chat_id: str, data: dict[str, Any]) -> None:
        """Handle incoming WebSocket message."""
        action = data.get("action", "chat")

        if action == "chat":
            content = data.get("message", "")
            media_paths: list[str] = []

            # Handle base64-encoded media items
            media_items = data.get("media", [])
            if media_items:
                try:
                    import base64

                    from pocketclaw.bus.media import build_media_hint, get_media_downloader

                    downloader = get_media_downloader()
                    names = []
                    for item in media_items:
                        b64_data = item.get("data", "")
                        name = item.get("name", "upload")
                        mime = item.get("mime_type")
                        if not b64_data:
                            continue
                        try:
                            raw = base64.b64decode(b64_data)
                            path = await downloader.save_from_bytes(raw, name, mime)
                            media_paths.append(path)
                            names.append(name)
                        except Exception as e:
                            logger.warning("Failed to save WebSocket media: %s", e)
                    if names:
                        content += build_media_hint(names)
                except Exception as e:
                    logger.warning("WebSocket media error: %s", e)

            message = InboundMessage(
                channel=Channel.WEBSOCKET,
                sender_id=chat_id,
                chat_id=chat_id,
                content=content,
                media=media_paths,
                metadata=data,
            )

            # Send stream_start to frontend to initialize the response UI
            ws = self._connections.get(chat_id)
            if ws:
                try:
                    await ws.send_json({"type": "stream_start"})
                except Exception:
                    pass

            await self._publish_inbound(message)
        # Other actions (settings, tools) handled separately

    async def send(self, message: OutboundMessage) -> None:
        """Send message to WebSocket client."""
        ws = self._connections.get(message.chat_id)
        if not ws:
            # Broadcast to all if no specific chat_id
            for ws in self._connections.values():
                await self._send_to_socket(ws, message)
        else:
            await self._send_to_socket(ws, message)

    async def _send_to_socket(self, ws: WebSocket, message: OutboundMessage) -> None:
        """Send to a specific WebSocket."""
        try:
            if message.is_stream_end:
                await ws.send_json({"type": "stream_end"})
                return

            await ws.send_json(
                {
                    "type": "message",
                    "content": message.content,
                    "is_stream_chunk": message.is_stream_chunk,
                    "metadata": message.metadata,
                }
            )
        except Exception as e:
            logger.warning("WebSocket send failed: %s", e)

    async def broadcast(self, content: Any, msg_type: str = "notification") -> None:
        """Broadcast to all connected clients."""
        for ws in self._connections.values():
            try:
                await ws.send_json({"type": msg_type, "content": content})
            except Exception:
                pass
