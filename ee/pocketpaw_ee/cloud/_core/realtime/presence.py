"""Cluster-wide presence for running the web tier as N processes.

``ConnectionManager`` only knows the sockets THIS process accepted. With
``POCKETPAW_REALTIME_BUS=redis-streams`` (see ``broadcast.py``) the web tier
may be several processes, so "is this user online" and "was that the user's
first / last connection" have to be answered across all of them. This module
answers them from a Redis sorted set per user:

- key ``presence:{user_id}``, one member per open socket,
  ``{process_id}:{conn_id}``, scored by the owning process's last heartbeat.
- every process re-scores its own members every ``HEARTBEAT_SECONDS``;
  members older than ``STALE_SECONDS`` are ignored and pruned, so a crashed
  process's sockets age out without anyone cleaning up after it.

The router and push dispatch call the module functions (``connect``,
``disconnect``, ``is_online``, ``online_among``, ``is_online_elsewhere``) with
the process's ``ConnectionManager``. With the realtime bus in ``inprocess``
mode there is no registry, the functions reduce to the manager's own
process-local answers, and no Redis call is made.

Invariants a reader must not break:

- Connect and disconnect each run as ONE Lua script (add-then-count,
  remove-then-count), so two processes racing on the same user produce exactly
  one ``presence.online`` / ``presence.offline`` between them.
- ``disconnect`` removes the member even when the manager has already
  forgotten the socket (``send_to_user`` prunes dead sockets directly);
  otherwise the heartbeat would keep refreshing a ghost forever.
- A Redis failure never fails the socket: every function falls back to the
  manager's process-local answer.
- A crashed process's users drop out of ``is_online`` after ``STALE_SECONDS``,
  but no ``presence.offline`` is emitted for them; peers correct on their next
  connect snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket as _socket
import time
import uuid
from typing import Any

from pocketpaw_ee.cloud._core.realtime import broadcast
from pocketpaw_ee.cloud._core.redis_client import get_redis

logger = logging.getLogger(__name__)

PRESENCE_PREFIX = "presence:"
HEARTBEAT_SECONDS = 30.0
STALE_SECONDS = 90.0

# KEYS[1] presence key. ARGV: member, now, stale_before, key_ttl_seconds.
# Returns the number of live members after adding this one.
_CONNECT_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. ARGV[3])
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return redis.call('ZCARD', KEYS[1])
"""

# KEYS[1] presence key. ARGV: member, stale_before.
# Returns the number of live members left after removing this one.
_DISCONNECT_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. ARGV[2])
local left = redis.call('ZCARD', KEYS[1])
if left == 0 then redis.call('DEL', KEYS[1]) end
return left
"""


def _new_process_id() -> str:
    return f"{_socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class Registry:
    """One process's view of the shared presence sets: its own members plus
    reads of everyone's."""

    def __init__(
        self,
        redis=None,
        *,
        prefix: str = PRESENCE_PREFIX,
        process_id: str | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        stale_seconds: float = STALE_SECONDS,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self.process_id = process_id or _new_process_id()
        self._heartbeat = heartbeat_seconds
        self._stale = stale_seconds
        # socket -> (user_id, member). Only this process's sockets.
        self._members: dict[Any, tuple[str, str]] = {}
        self._task: asyncio.Task | None = None

    def _r(self):
        return self._redis if self._redis is not None else get_redis()

    def _key(self, user_id: str) -> str:
        return f"{self._prefix}{user_id}"

    def _key_ttl(self) -> int:
        return int(self._stale * 2) + 1

    async def add(self, ws: Any, user_id: str) -> int:
        """Register ``ws``; return the user's live connection count cluster-wide."""
        member = f"{self.process_id}:{uuid.uuid4().hex}"
        self._members[ws] = (user_id, member)
        now = time.time()
        return int(
            await self._r().eval(
                _CONNECT_LUA, 1, self._key(user_id), member, now, now - self._stale, self._key_ttl()
            )
        )

    async def remove(self, ws: Any) -> tuple[str, int] | None:
        """Drop ``ws``; return ``(user_id, live connections left)``, or ``None``
        when this process never registered it."""
        entry = self._members.pop(ws, None)
        if entry is None:
            return None
        user_id, member = entry
        left = await self._r().eval(
            _DISCONNECT_LUA, 1, self._key(user_id), member, time.time() - self._stale
        )
        return user_id, int(left)

    async def remote_online(self, user_ids: list[str]) -> set[str]:
        """The users among ``user_ids`` with a live socket on ANOTHER process."""
        if not user_ids:
            return set()
        mine = f"{self.process_id}:"
        pipe = self._r().pipeline(transaction=False)
        for uid in user_ids:
            pipe.zrangebyscore(self._key(uid), time.time() - self._stale, "+inf")
        rows = await pipe.execute()
        return {
            uid
            for uid, members in zip(user_ids, rows, strict=True)
            if any(not str(m).startswith(mine) for m in members or ())
        }

    async def heartbeat_once(self) -> None:
        """Re-score every socket this process holds."""
        if not self._members:
            return
        now = time.time()
        pipe = self._r().pipeline(transaction=False)
        for user_id, member in list(self._members.values()):
            key = self._key(user_id)
            pipe.zadd(key, {member: now})
            pipe.expire(key, self._key_ttl())
        await pipe.execute()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat)
            try:
                await self.heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("presence heartbeat failed", exc_info=True)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop heartbeating and drop this process's members (clean shutdown)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        entries, self._members = list(self._members.values()), {}
        if not entries:
            return
        try:
            pipe = self._r().pipeline(transaction=False)
            for user_id, member in entries:
                pipe.zrem(self._key(user_id), member)
            await pipe.execute()
        except Exception:
            logger.debug("presence cleanup on shutdown failed", exc_info=True)


# --- module singleton ----------------------------------------------------------

_registry: Registry | None = None


def active_registry() -> Registry | None:
    """This process's registry when the realtime bus is redis-streams, else None."""
    global _registry
    if not broadcast.is_enabled():
        return None
    if _registry is None:
        _registry = Registry()
    return _registry


def _for(manager) -> Registry | None:
    # Tests simulating several processes give each manager its own registry.
    registry = getattr(manager, "presence_registry", None)
    return registry if isinstance(registry, Registry) else active_registry()


async def start() -> None:
    registry = active_registry()
    if registry is not None:
        await registry.start()


async def stop() -> None:
    if _registry is not None:
        await _registry.stop()


def _reset_for_tests() -> None:
    global _registry
    _registry = None


# --- the calls the router and push dispatch make --------------------------------


async def connect(manager, ws, user_id: str) -> bool:
    """Register ``ws`` with the manager (and the cluster registry); return True
    when this is the user's first live connection anywhere."""
    was_offline_here = not manager.is_online(user_id)
    await manager.connect(ws, user_id)
    registry = _for(manager)
    if registry is None:
        return was_offline_here
    try:
        return await registry.add(ws, user_id) == 1
    except Exception:
        logger.warning("presence connect failed; using this process's view", exc_info=True)
        return was_offline_here


async def disconnect(manager, ws) -> str | None:
    """Unregister ``ws``; return the user id when that was their last live
    connection anywhere (the caller starts the offline grace timer)."""
    last_here = await manager.disconnect(ws)
    registry = _for(manager)
    if registry is None:
        return last_here
    try:
        removed = await registry.remove(ws)
    except Exception:
        logger.warning("presence disconnect failed; using this process's view", exc_info=True)
        return last_here
    if removed is None:
        return last_here
    user_id, left = removed
    return user_id if left == 0 else None


async def online_among(manager, user_ids: list[str]) -> set[str]:
    """The users among ``user_ids`` with a live connection on any process."""
    online = {uid for uid in user_ids if manager.is_online(uid)}
    registry = _for(manager)
    rest = [uid for uid in user_ids if uid not in online]
    if registry is None or not rest:
        return online
    try:
        return online | await registry.remote_online(rest)
    except Exception:
        logger.warning("presence read failed; using this process's view", exc_info=True)
        return online


async def is_online(manager, user_id: str) -> bool:
    return user_id in await online_among(manager, [user_id])


async def is_online_elsewhere(manager, user_id: str) -> bool:
    """True when another process holds a live socket for the user. Always
    False in inprocess mode."""
    registry = _for(manager)
    if registry is None:
        return False
    try:
        return user_id in await registry.remote_online([user_id])
    except Exception:
        logger.warning("presence read failed; treating the user as not elsewhere", exc_info=True)
        return False
