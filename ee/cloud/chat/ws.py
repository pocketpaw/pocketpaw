"""WebSocket connection manager for real-time chat.

Single endpoint: ws://host/ws/cloud?token=<JWT>

Handles:
- Connection lifecycle (connect -> authenticate -> active -> disconnect)
- User-to-connections mapping: user_id -> set[WebSocket] (multi-tab/device)
- Message routing to group members
- Typing indicators with auto-expiry (5s)
- Presence tracking with grace period (30s before marking offline)
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

from ee.cloud.chat.schemas import WsOutbound

logger = logging.getLogger(__name__)

TYPING_TIMEOUT_SECONDS = 5
PRESENCE_GRACE_SECONDS = 30


class ConnectionManager:
    """Manages WebSocket connections, presence, and message routing."""

    def __init__(self) -> None:
        # user_id -> set of WebSocket connections
        self.active_connections: dict[str, set[WebSocket]] = {}
        # ws -> user_id (reverse lookup)
        self._ws_to_user: dict[WebSocket, str] = {}
        # Pending offline tasks (grace period before marking offline)
        self._offline_tasks: dict[str, asyncio.Task] = {}
        # Typing timers: (group_id, user_id) -> Task
        self._typing_timers: dict[tuple[str, str], asyncio.Task] = {}

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        """Register an authenticated WebSocket connection."""
        if user_id not in self.active_connections:
            self.active_connections[user_id] = set()
        self.active_connections[user_id].add(websocket)
        self._ws_to_user[websocket] = user_id

        # Cancel any pending offline task
        task = self._offline_tasks.pop(user_id, None)
        if task:
            task.cancel()

        logger.info(
            "WS connected: user=%s (connections=%d)",
            user_id,
            len(self.active_connections[user_id]),
        )

    async def disconnect(self, websocket: WebSocket) -> str | None:
        """Remove a connection.

        Returns the user_id if this was their last connection (the caller
        should start a grace-period offline timer).  Returns ``None`` if the
        user still has other active connections or the websocket was unknown.
        """
        user_id = self._ws_to_user.pop(websocket, None)
        if not user_id:
            return None

        conns = self.active_connections.get(user_id, set())
        conns.discard(websocket)

        if not conns:
            # Last connection gone — return user_id for grace period handling
            del self.active_connections[user_id]
            return user_id

        return None

    def get_user_connections(self, user_id: str) -> set[WebSocket]:
        """Return the set of active WebSocket connections for a user."""
        return self.active_connections.get(user_id, set())

    def is_online(self, user_id: str) -> bool:
        """Check whether a user has at least one active connection."""
        return bool(self.active_connections.get(user_id))

    async def send_to_user(self, user_id: str, message: WsOutbound) -> None:
        """Send a message to all of a user's connections."""
        data = message.model_dump(mode="json")
        dead: list[WebSocket] = []
        for ws in self.get_user_connections(user_id):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        # Clean up dead connections
        for ws in dead:
            await self.disconnect(ws)

    async def broadcast_to_group(
        self,
        group_id: str,
        member_ids: list[str],
        message: WsOutbound,
        exclude_user: str | None = None,
    ) -> None:
        """Broadcast a message to all online members of a group."""
        for uid in member_ids:
            if uid == exclude_user:
                continue
            await self.send_to_user(uid, message)

    # ------------------------------------------------------------------
    # Typing indicators
    # ------------------------------------------------------------------

    def start_typing(self, group_id: str, user_id: str) -> None:
        """Track typing with auto-expiry."""
        key = (group_id, user_id)
        # Cancel existing timer
        existing = self._typing_timers.pop(key, None)
        if existing:
            existing.cancel()
        # Start new timer
        self._typing_timers[key] = asyncio.create_task(self._typing_timeout(key))

    async def _typing_timeout(self, key: tuple[str, str]) -> None:
        """Auto-expire typing indicator after TYPING_TIMEOUT_SECONDS."""
        await asyncio.sleep(TYPING_TIMEOUT_SECONDS)
        self._typing_timers.pop(key, None)

    def stop_typing(self, group_id: str, user_id: str) -> None:
        """Explicitly stop a typing indicator."""
        key = (group_id, user_id)
        task = self._typing_timers.pop(key, None)
        if task:
            task.cancel()

    def is_typing(self, group_id: str, user_id: str) -> bool:
        """Check whether a user is currently typing in a group."""
        return (group_id, user_id) in self._typing_timers


# Module-level singleton
manager = ConnectionManager()
