"""WebSocket connection manager for real-time chat.

Single endpoint: ws://host/ws/cloud?token=<JWT>. The manager owns the sockets
THIS process holds: user -> sockets (multi-tab/device), socket -> current room,
typing timers (5s auto-expiry) and presence grace timers (30s before offline).

Fan-out (``send_to_user``, ``broadcast_to_group``, ``send_to_room``) sends
concurrently under a bounded gather, each send capped by
``SEND_TIMEOUT_SECONDS``, so one back-pressured tab costs the slowest recipient,
not the sum. A timed-out or failing socket is dropped, never retried.

Cross-process: with ``POCKETPAW_REALTIME_BUS=redis-streams``,
``broadcast_to_group`` and ``send_to_room`` also relay the frame through
``_core/realtime/broadcast.py`` so other web processes reach their own sockets.
``send_to_user`` never relays: its return value counts THIS process's accepting
sockets. ``is_online`` and typing state are process-local; the cluster-wide
presence answer (first/last connection, online elsewhere) lives in
``_core/realtime/presence.py``, which the router and ``push/dispatch.py`` call
with this manager.

Liveness is capability-gated. A socket that has sent a ``ping`` is marked
ping-capable and is live only while INBOUND traffic arrives inside
``LIVENESS_STALE_SECONDS``; outbound sends are not evidence, because a write to
a half-open socket succeeds for minutes into the kernel buffer. A stale capable
socket is skipped by fan-out, not counted as delivered, and closed best-effort,
which wakes the receive loop so the normal disconnect/presence path runs.
Sockets that never ping (older clients) keep the "live while registered"
verdict; their safety net is the zero-accept fallback in ``push/dispatch.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import WebSocket

from pocketpaw_ee.cloud._core.realtime import broadcast
from pocketpaw_ee.cloud._core.realtime.fanout import map_bounded
from pocketpaw_ee.cloud.chat.schemas import WsOutbound

logger = logging.getLogger(__name__)

TYPING_TIMEOUT_SECONDS = 5
PRESENCE_GRACE_SECONDS = 30
# A ping-capable socket silent this long is presumed dead. The client pings
# every 30s foregrounded, but Chrome's intensive throttling slows hidden tabs'
# timers to ~1/min after 5 minutes — so size for two missed pings at the
# THROTTLED cadence, or backgrounded tabs (exactly where push matters) would
# sit on the boundary and flap close(1001)/reconnect. Costs only slower zombie
# detection. Sockets that don't ping are exempt entirely (see _is_fresh).
LIVENESS_STALE_SECONDS = 150
# Upper bound on a single ``send_json``. uvicorn (ws=auto → the ``websockets``
# protocol) awaits ``drain()`` inside ``send()`` with a 64 KiB write high-water
# mark, so a client that stopped reading (backgrounded phone) eventually
# back-pressures the write. Fan-out is awaited per audience member INLINE in
# the emitting request (InProcessBus.publish → send_to_user), so one stuck
# socket would stall delivery to every other member and the sender's own
# request. On timeout the socket is DROPPED, never retried: ``websockets``
# documents that cancelling ``send()`` mid-drain leaves the connection
# unusable. The client reconnects (the FE bus reconnects on any close code
# other than 1000/4001 and refetches on ``system.reconnected``). 5s is far
# above any healthy RTT and short enough that a stalled fan-out is noticed.
SEND_TIMEOUT_SECONDS = 5.0


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
        # ws -> monotonic timestamp of the last INBOUND frame. This is the only
        # input to the freshness verdict; see _is_fresh for why outbound is not.
        self._ws_last_inbound: dict[WebSocket, float] = {}
        # ws -> monotonic timestamp of the last outbound send the socket
        # accepted. Diagnostics only — deliberately NOT part of freshness.
        self._ws_last_outbound: dict[WebSocket, float] = {}
        # Sockets that have proved they speak the ping heartbeat. Only these
        # are subject to staleness; everything else keeps legacy semantics.
        self._ping_capable: set[WebSocket] = set()
        # Sockets with a close already in flight, so repeated is_online calls
        # don't pile up duplicate close tasks for the same socket.
        self._closing: set[WebSocket] = set()
        # Strong refs to in-flight close tasks — a bare create_task result is
        # only weakly held by the loop and can be GC'd mid-await.
        self._close_tasks: set[asyncio.Task] = set()
        # Explicit cross-process broadcast channel, for tests that simulate
        # several web processes in one; otherwise the process's own channel is
        # used, and exists only when broadcast is on (see broadcast.py).
        self.relay_channel: broadcast.Channel | None = None
        # Same idea for the cluster presence registry (see presence.py).
        self.presence_registry = None

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
        self._ws_last_inbound.pop(websocket, None)
        self._ws_last_outbound.pop(websocket, None)
        self._ping_capable.discard(websocket)
        self._closing.discard(websocket)

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

    def mark_ping_capable(self, websocket: WebSocket) -> None:
        """Record that this socket speaks the ping heartbeat.

        Called from the router's ``ping`` branch, so capability is proved by
        observed behaviour rather than assumed from a version string. Only
        capable sockets are subject to staleness — see :meth:`_is_fresh`.
        Idempotent; untracked sockets are ignored.
        """
        if websocket in self._ws_to_user:
            self._ping_capable.add(websocket)

    def touch(self, websocket: WebSocket) -> None:
        """Record an INBOUND frame, refreshing the socket's liveness window.

        Called by the router's receive loop on every inbound frame (the ping
        heartbeat included). Untracked sockets are ignored — a socket that has
        already been disconnected must not resurrect itself here.
        """
        if websocket in self._ws_to_user:
            self._ws_last_inbound[websocket] = time.monotonic()

    def _mark_outbound(self, websocket: WebSocket) -> None:
        """Record an outbound send the socket accepted. Diagnostics only.

        Explicitly NOT an input to :meth:`_is_fresh` — see the note there on
        why a successful write proves nothing about a half-open socket.
        """
        if websocket in self._ws_to_user:
            self._ws_last_outbound[websocket] = time.monotonic()

    def _is_fresh(self, websocket: WebSocket, *, now: float) -> bool:
        """True when the socket should be treated as live.

        Two regimes, keyed on whether the client has proved it pings:

        - **Ping-capable** — freshness is INBOUND traffic only. Outbound sends
          are not evidence: ``send_json`` to a half-open socket succeeds for
          minutes (the kernel just buffers the write), so counting them would
          let workspace fan-out resurrect a zombie forever in a busy workspace,
          which is the exact bug this liveness work exists to kill.
        - **Not ping-capable** — legacy semantics: live while registered. Old
          FE bundles send nothing for long stretches while perfectly alive, so
          staleness would churn them into a close/reconnect loop. Their safety
          net stays the dispatch zero-accept fallback, exactly as before this
          change — no regression, and no ordering constraint against the FE
          ping rollout.
        """
        if websocket not in self._ping_capable:
            return True

        last_inbound = self._ws_last_inbound.get(websocket)
        if last_inbound is None:
            # Capable but never stamped — treat as stale rather than trusting it.
            return False
        return (now - last_inbound) < LIVENESS_STALE_SECONDS

    def _close_stale(self, websocket: WebSocket) -> None:
        """Best-effort close of a presumed-zombie socket. Never raises.

        Deliberately does NOT unregister the socket: closing it wakes the
        router's ``receive_text()`` loop, which runs the normal ``disconnect``
        path and the presence grace timer with it. Unregistering here instead
        would swallow the ``presence.offline`` broadcast.
        """
        if websocket in self._closing:
            return  # A close is already in flight for this socket.

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No loop (sync context) — the next send will prune it.

        async def _close() -> None:
            with contextlib.suppress(Exception):
                await websocket.close(code=1001, reason="Stale connection")

        try:
            task = loop.create_task(_close())
        except Exception:
            return
        # Hold a strong ref until the task finishes: the event loop only keeps
        # a weak one, so an unreferenced task can be collected mid-close.
        self._closing.add(websocket)
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)
        task.add_done_callback(lambda _t: self._closing.discard(websocket))

    async def _send_bounded(self, websocket: WebSocket, data: dict) -> bool:
        """Send one frame, bounded by ``SEND_TIMEOUT_SECONDS``.

        Returns False when the socket must be treated as dead — a raised send
        (broken pipe, closed socket) OR a timeout. A timed-out send is not
        retried on the same socket: ``websockets`` documents that cancelling
        ``send()`` mid-drain leaves the connection unusable, so the caller
        drops it and the client reconnects. See ``SEND_TIMEOUT_SECONDS``.
        """
        try:
            await asyncio.wait_for(websocket.send_json(data), timeout=SEND_TIMEOUT_SECONDS)
        except TimeoutError:
            # asyncio.TimeoutError IS builtins.TimeoutError on 3.11+ (the
            # project floor), so the builtin alone covers wait_for's raise;
            # ruff UP041 rejects the redundant alias.
            logger.warning(
                "WS send timed out after %.1fs; dropping socket user=%s",
                SEND_TIMEOUT_SECONDS,
                self._ws_to_user.get(websocket),
            )
            # Unlike a raised send, a timed-out socket is still OPEN: the
            # router's receive loop keeps answering the client's pings
            # directly, so the client would think it is healthy while it no
            # longer receives fan-out (the caller unregisters it). Tear the
            # transport down best-effort — uvicorn's close() aborts the
            # transport after its handshake timeout even if the write side is
            # stuck — so the receive loop exits and the client reconnects.
            self._close_stale(websocket)
            return False
        except Exception:
            return False
        return True

    def is_online(self, user_id: str) -> bool:
        """Check whether a user has at least one LIVE connection.

        For a ping-capable client "live" means an inbound frame inside
        ``LIVENESS_STALE_SECONDS``, not merely a socket object in the registry
        — see :meth:`_is_fresh` and the module docstring. Stale sockets are
        closed best-effort on the way through, so the verdict and the cleanup
        converge without a separate sweeper.
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
        and got pruned, or stale and skipped) — the signal
        ``push.dispatch.notify`` uses to fall back to Web Push instead of
        dropping the notification.
        """
        data = message.model_dump(mode="json")
        now = time.monotonic()
        # Iterate a COPY: a concurrent _close_stale → disconnect mutates the
        # live set, and the resulting "set changed size during iteration" fires
        # at the for statement — outside the per-send try — so it would escape
        # to notify() and be mistaken for a WS failure, skipping the fallback.
        fresh: list[WebSocket] = []
        for ws in list(self.get_user_connections(user_id)):
            # Same verdict as is_online: a stale ping-capable socket is a
            # zombie. Writing to it "succeeds" (kernel buffer) and would count
            # as delivered — which is exactly the signal dispatch reads to skip
            # Web Push. Skip it and close it best-effort instead; the receive
            # loop then runs the normal disconnect/presence path.
            if not self._is_fresh(ws, now=now):
                self._close_stale(ws)
            else:
                fresh.append(ws)
        if not fresh:
            return 0

        # One user's sockets are their open tabs and devices. Sending serially
        # made a single back-pressured tab cost every OTHER device on this user
        # the full SEND_TIMEOUT_SECONDS before its own frame was even
        # attempted. _send_bounded contains its own failures and returns a
        # bool, so nothing raises out of the gather.
        results = await map_bounded(fresh, lambda ws: self._send_bounded(ws, data))

        delivered = 0
        dead: list[WebSocket] = []
        for ws, accepted in zip(fresh, results, strict=True):
            if accepted:
                delivered += 1
                self._mark_outbound(ws)
            else:
                dead.append(ws)
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
        *,
        relay: bool = True,
    ) -> None:
        """Broadcast a message to all online members of a group.

        With cross-process broadcast on, the frame is also relayed so the other
        web processes reach the members whose sockets they hold. ``relay=False``
        is for the broadcast consumer itself, delivering a frame another
        process already relayed.
        """
        targets = [uid for uid in member_ids if uid != exclude_user]
        # Concurrent for the same reason as send_to_user, one level up: a
        # group's members are independent recipients, so the broadcast should
        # cost the slowest member rather than all of them added together.
        await map_bounded(targets, lambda uid: self.send_to_user(uid, message))
        channel = (self.relay_channel or broadcast.active_channel()) if relay else None
        if channel is not None and targets:
            await channel.publish_group(
                group_id, targets, message.type, message.model_dump(mode="json")["data"]
            )

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
        relay: bool = True,
    ) -> None:
        """Send to every socket currently joined to the room.

        Does not know group membership — membership was enforced at join time
        by the handler (the router dispatcher validates the joiner is allowed
        in the group before calling ``join_room``). Room membership is per
        process, so with cross-process broadcast on the frame is relayed and
        each process sends to its own joined sockets; ``relay=False`` is for the
        broadcast consumer.
        """
        data = message.model_dump(mode="json")
        channel = (self.relay_channel or broadcast.active_channel()) if relay else None
        if channel is not None:
            await channel.publish_room(group_id, message.type, data["data"], exclude_user)
        now = time.monotonic()
        fresh: list[WebSocket] = []
        for ws, room in list(self._ws_to_room.items()):
            if room != group_id:
                continue
            if exclude_user and self._ws_to_user.get(ws) == exclude_user:
                continue
            # Same freshness gate as send_to_user — see the note there.
            if not self._is_fresh(ws, now=now):
                self._close_stale(ws)
            else:
                fresh.append(ws)
        if not fresh:
            return

        results = await map_bounded(fresh, lambda ws: self._send_bounded(ws, data))

        dead: list[WebSocket] = []
        for ws, accepted in zip(fresh, results, strict=True):
            if accepted:
                self._mark_outbound(ws)
            else:
                dead.append(ws)
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
