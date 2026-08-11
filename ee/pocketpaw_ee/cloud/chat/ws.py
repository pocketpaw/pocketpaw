"""WebSocket connection manager for real-time chat.

Single endpoint: ws://host/ws/cloud?token=<JWT>

Handles:
- Connection lifecycle (connect -> authenticate -> active -> disconnect)
- User-to-connections mapping: user_id -> set[WebSocket] (multi-tab/device)
- Message routing to group members
- Typing indicators with auto-expiry (5s)
- Presence tracking with grace period (30s before marking offline)
- Per-socket liveness (see below)

Updated: 2026-08-11 (fix/notif-liveness-dispatch) — ``is_online`` used to mean
"a socket object is present in the dict", which a half-open socket (laptop
asleep, NAT timeout) satisfies indefinitely: the TCP close never arrives, so
the registry keeps a socket nobody is listening on. Notification dispatch
reads that verdict to pick WS over Web Push, so a zombie socket silently ate
the notification. Liveness is now traffic-based: every socket carries a
last-activity stamp, refreshed on any inbound frame (``touch``, called from
the router's receive loop) and on any successful outbound send. A socket with
no traffic in EITHER direction for ``LIVENESS_STALE_SECONDS`` is presumed
zombie — excluded from the verdict and closed best-effort, which wakes the
router's receive loop so the normal disconnect/presence path runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import WebSocket

from pocketpaw_ee.cloud.chat.schemas import WsOutbound

logger = logging.getLogger(__name__)

TYPING_TIMEOUT_SECONDS = 5
PRESENCE_GRACE_SECONDS = 30
# A socket with no traffic in either direction for this long is presumed dead.
# Comfortably above the ~30s cadence of ordinary workspace chatter (presence
# deltas, chat fan-out, client pings) so an idle-but-healthy socket in an
# active workspace stays fresh on outbound traffic alone.
LIVENESS_STALE_SECONDS = 60


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
        # Current room per socket (at most one): ws -> group_id
        self._ws_to_room: dict[WebSocket, str] = {}
        # ws -> monotonic timestamp of the last traffic in EITHER direction
        # (inbound frame received, or outbound send the socket accepted).
        self._ws_last_seen: dict[WebSocket, float] = {}

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        """Register an authenticated WebSocket connection."""
        if user_id not in self.active_connections:
            self.active_connections[user_id] = set()
        self.active_connections[user_id].add(websocket)
        self._ws_to_user[websocket] = user_id
        self.touch(websocket)

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
        # Always clear any room association, regardless of user mapping.
        self._ws_to_room.pop(websocket, None)
        self._ws_last_seen.pop(websocket, None)

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

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------

    def touch(self, websocket: WebSocket) -> None:
        """Record traffic on a socket, refreshing its liveness window.

        Called by the router's receive loop on EVERY inbound frame (including
        the client's ``ping`` heartbeat) and internally on every outbound send
        the socket accepted. Untracked sockets are ignored — a socket that has
        already been disconnected must not resurrect itself here.
        """
        if websocket in self._ws_to_user:
            self._ws_last_seen[websocket] = time.monotonic()

    def _is_fresh(self, websocket: WebSocket, *, now: float) -> bool:
        """True when the socket has seen traffic inside the liveness window."""
        last_seen = self._ws_last_seen.get(websocket)
        if last_seen is None:
            # Tracked but never stamped — treat as stale rather than trusting it.
            return False
        return (now - last_seen) < LIVENESS_STALE_SECONDS

    def _close_stale(self, websocket: WebSocket) -> None:
        """Best-effort close of a presumed-zombie socket. Never raises.

        Deliberately does NOT unregister the socket: closing it wakes the
        router's ``receive_text()`` loop, which runs the normal ``disconnect``
        path and the presence grace timer with it. Unregistering here instead
        would swallow the ``presence.offline`` broadcast.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No loop (sync context) — the next send will prune it.

        async def _close() -> None:
            with contextlib.suppress(Exception):
                await websocket.close(code=1001, reason="Stale connection")

        with contextlib.suppress(Exception):
            loop.create_task(_close())

    def is_online(self, user_id: str) -> bool:
        """Check whether a user has at least one LIVE connection.

        "Live" means traffic inside ``LIVENESS_STALE_SECONDS``, not merely a
        socket object in the registry — see the module docstring. Sockets past
        the window are closed best-effort on the way through, so the verdict
        and the cleanup converge without a separate sweeper.
        """
        conns = self.active_connections.get(user_id)
        if not conns:
            return False

        now = time.monotonic()
        live = False
        for ws in list(conns):
            if self._is_fresh(ws, now=now):
                live = True
            else:
                self._close_stale(ws)
        return live

    async def send_to_user(self, user_id: str, message: WsOutbound) -> int:
        """Send a message to all of a user's connections.

        Returns the number of sockets that ACCEPTED the frame. Zero means the
        user looked connected but nothing was delivered (every socket was dead
        and got pruned) — the signal ``push.dispatch.notify`` uses to fall back
        to Web Push instead of dropping the notification.
        """
        data = message.model_dump(mode="json")
        delivered = 0
        dead: list[WebSocket] = []
        for ws in self.get_user_connections(user_id):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
            else:
                delivered += 1
                # A send the socket accepted is proof of life for the liveness
                # window — idle-but-healthy clients that never send anything
                # inbound stay online on outbound traffic alone.
                self.touch(ws)
        # Clean up dead connections
        for ws in dead:
            await self.disconnect(ws)
        return delivered

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
    # Room tracking (at most one current room per socket)
    # ------------------------------------------------------------------

    def join_room(self, websocket: WebSocket, group_id: str) -> None:
        """Associate a socket with a single current room. Replaces any prior room."""
        self._ws_to_room[websocket] = group_id

    def leave_room(self, websocket: WebSocket) -> None:
        """Clear the socket's current room. Idempotent."""
        self._ws_to_room.pop(websocket, None)

    def current_room(self, websocket: WebSocket) -> str | None:
        """Return the socket's current room, or None if not in any room."""
        return self._ws_to_room.get(websocket)

    async def send_to_room(
        self,
        group_id: str,
        message: WsOutbound,
        *,
        exclude_user: str | None = None,
    ) -> None:
        """Send to every socket currently joined to the room.

        Does not know group membership — membership was enforced at join time
        by the handler (the router dispatcher validates the joiner is allowed
        in the group before calling ``join_room``).
        """
        data = message.model_dump(mode="json")
        dead: list[WebSocket] = []
        for ws, room in list(self._ws_to_room.items()):
            if room != group_id:
                continue
            if exclude_user and self._ws_to_user.get(ws) == exclude_user:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
            else:
                self.touch(ws)
        for ws in dead:
            await self.disconnect(ws)

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
