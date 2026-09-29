"""Cross-process WebSocket broadcast for running the web tier as N processes.

Every web process holds its own sockets, so a frame emitted on process A must
also be delivered by processes B..N to the sockets they hold. This module is
that path: a Redis Stream (``cloud:realtime:broadcast``) read by a PER-PROCESS
consumer group, so every process sees every entry (broadcast). It is the
counterpart of ``xproc``'s shared ``cloud-web`` group, which gives bus
envelopes exactly-once delivery; the two kinds need opposite semantics, see
``docs/design/plans/pocketpaw/2026-09-04-redis-utilization-audit.md`` §1.

Off by default. ``init_realtime`` calls ``configure(enabled=True)`` only for
``POCKETPAW_REALTIME_BUS=redis-streams``; while disabled every entry point is a
single module-bool check, so a single-process deploy pays nothing.

Envelope kinds on the stream:

- ``ws``    — ``broadcast_to_group(scope_id, recipients, frame)`` on each process.
- ``room``  — ``send_to_room(group_id, frame, exclude_user)`` on each process.
- ``cache.invalidate`` — run a registered invalidator (``register_invalidator``).

Invariants a reader must not break:

- A process skips entries whose ``origin`` is its own id; it already delivered
  them locally. Removing that check double-delivers every web-originated frame.
- Remote dispatch calls the connection manager with ``relay=False``; relaying
  again would loop every frame around the cluster forever.
- Bus handlers are never relayed. Only socket frames and cache invalidations
  cross this stream; side effects stay on whichever process published.
- Delivery is at-most-once (``NOACK``): a process that lags past ``MAXLEN`` or
  restarts misses frames, which the client recovers from on reconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from redis import exceptions as redis_exceptions

from pocketpaw_ee.cloud._core.redis_client import get_redis

logger = logging.getLogger(__name__)

BROADCAST_STREAM = "cloud:realtime:broadcast"
GROUP_PREFIX = "web-"
BLOCK_MS = 15000
BATCH = 64
# Every process reads every entry, so the cap bounds memory, not backlog; a
# process that falls this far behind drops frames rather than replaying them.
MAXLEN = 10000
# A live consumer re-enters XREADGROUP at least every BLOCK_MS, so its idle time
# stays near zero. Ten minutes of silence means the process is gone.
STALE_GROUP_MS = 10 * 60 * 1000
CLEANUP_INTERVAL_SECONDS = 60.0
_CONSUMER = "c"

Invalidator = Callable[[str], Any]

#: Process-wide invalidator registry: name -> fn(key). Owning modules register
#: at import time, so the registry is populated wherever the cache exists.
_INVALIDATORS: dict[str, Invalidator] = {}


def register_invalidator(name: str, fn: Invalidator) -> None:
    """Register ``fn(key)`` to run on every process when ``name`` is invalidated."""
    _INVALIDATORS[name] = fn


def _new_group_name() -> str:
    # The creation time is in the name so cleanup can tell a crashed process's
    # group (no consumers, old) from one created a moment ago (no consumers yet).
    return f"{GROUP_PREFIX}{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _created_ms(group: str) -> int | None:
    try:
        return int(group[len(GROUP_PREFIX) :].split("-", 1)[0])
    except ValueError:
        return None


def _lane(envelope: dict) -> tuple[str, str]:
    """Ordering lane: entries in one lane dispatch in stream order.

    ``ws`` shares ``xproc``'s rule (scope id) because it carries the same
    streamed agent chunks, which must not be reordered.
    """
    kind = envelope.get("kind")
    if kind == "ws":
        return ("ws", str(envelope.get("scope_id", "")))
    if kind == "room":
        return ("room", str(envelope.get("group_id", "")))
    if kind == "cache.invalidate":
        return ("inv", str(envelope.get("name", "")))
    return ("unknown", str(kind))


class Channel:
    """One process's end of the broadcast stream: publisher and consumer."""

    def __init__(
        self,
        redis=None,
        *,
        stream: str = BROADCAST_STREAM,
        conn_manager=None,
        invalidators: dict[str, Invalidator] | None = None,
        stale_ms: int = STALE_GROUP_MS,
        block_ms: int = BLOCK_MS,
    ) -> None:
        self._redis = redis
        self.stream = stream
        self.group = _new_group_name()
        self._conn = conn_manager
        self._invalidators = _INVALIDATORS if invalidators is None else invalidators
        self._stale_ms = stale_ms
        self._block_ms = block_ms
        self._task: asyncio.Task | None = None

    # --- plumbing -----------------------------------------------------------

    def _r(self):
        return self._redis if self._redis is not None else get_redis()

    def _manager(self):
        if self._conn is None:
            # Lazy: chat.ws pulls FastAPI, and the worker never dispatches.
            from pocketpaw_ee.cloud.chat.ws import manager

            self._conn = manager
        return self._conn

    # --- publish ------------------------------------------------------------

    async def _publish(self, envelope: dict) -> None:
        envelope["origin"] = self.group
        try:
            await self._r().xadd(
                self.stream,
                {"envelope": json.dumps(envelope)},
                maxlen=MAXLEN,
                approximate=True,
            )
        except Exception:
            # Best-effort like the local fan-out: never fail the emitting request.
            logger.exception("realtime broadcast publish failed for %s", envelope.get("kind"))

    async def publish_group(
        self, scope_id: str, recipients: list[str], ws_type: str, ws_data: dict
    ) -> None:
        await self._publish(
            {
                "kind": "ws",
                "scope_id": scope_id,
                "recipients": list(recipients),
                "type": ws_type,
                "data": ws_data,
            }
        )

    async def publish_room(
        self, group_id: str, ws_type: str, ws_data: dict, exclude_user: str | None
    ) -> None:
        await self._publish(
            {
                "kind": "room",
                "group_id": group_id,
                "type": ws_type,
                "data": ws_data,
                "exclude_user": exclude_user,
            }
        )

    async def publish_invalidate(self, name: str, key: str) -> None:
        await self._publish({"kind": "cache.invalidate", "name": name, "key": key})

    # --- consume ------------------------------------------------------------

    async def _create_group(self) -> None:
        try:
            await self._r().xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except redis_exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def start(self) -> None:
        """Clean up dead processes' groups, create ours from ``$``, start reading."""
        if self._task is not None:
            return
        await self.cleanup_stale_groups()
        await self._create_group()
        self._task = asyncio.create_task(self._run())
        logger.info("realtime broadcast consumer %s reading %s", self.group, self.stream)

    async def stop(self) -> None:
        """Stop reading and destroy this process's group (clean shutdown only)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await self._r().xgroup_destroy(self.stream, self.group)
        except Exception:
            logger.debug("broadcast group destroy failed for %s", self.group, exc_info=True)

    async def cleanup_stale_groups(self) -> list[str]:
        """Destroy other processes' groups idle longer than ``stale_ms``.

        A group with consumers is judged by its most recently active consumer;
        one with none yet by the creation time in its name, so a sibling that
        has created its group but not yet read is never removed.
        """
        redis = self._r()
        try:
            groups = await redis.xinfo_groups(self.stream)
        except redis_exceptions.ResponseError:
            return []  # stream does not exist yet
        except Exception:
            logger.debug("broadcast cleanup: xinfo_groups failed", exc_info=True)
            return []
        now_ms = int(time.time() * 1000)
        removed: list[str] = []
        for info in groups:
            name = info.get("name")
            if not isinstance(name, str) or name == self.group or not name.startswith(GROUP_PREFIX):
                continue
            try:
                consumers = await redis.xinfo_consumers(self.stream, name)
                if consumers:
                    idle = min(int(c.get("idle", 0)) for c in consumers)
                else:
                    created = _created_ms(name)
                    if created is None:
                        continue
                    idle = now_ms - created
                if idle > self._stale_ms:
                    await redis.xgroup_destroy(self.stream, name)
                    removed.append(name)
            except Exception:
                logger.debug("broadcast cleanup failed for group %s", name, exc_info=True)
        if removed:
            logger.info("realtime broadcast removed %d stale group(s): %s", len(removed), removed)
        return removed

    async def _run(self) -> None:
        backoff = 1.0
        next_cleanup = time.monotonic() + CLEANUP_INTERVAL_SECONDS
        while True:
            try:
                resp = await self._r().xreadgroup(
                    self.group,
                    _CONSUMER,
                    {self.stream: ">"},
                    count=BATCH,
                    block=self._block_ms,
                    noack=True,
                )
            except asyncio.CancelledError:
                raise
            except redis_exceptions.ResponseError as exc:
                if "NOGROUP" in str(exc):
                    # A sibling's cleanup removed our group during a long stall,
                    # or the stream was deleted. Rejoin from now.
                    logger.warning("broadcast group %s vanished; recreating", self.group)
                    try:
                        await self._create_group()
                    except Exception:
                        logger.exception("broadcast group recreate failed")
                        await asyncio.sleep(backoff)
                    continue
                logger.exception("broadcast xreadgroup failed; backing off %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)
                continue
            except Exception:
                logger.exception("broadcast xreadgroup failed; backing off %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)
                continue

            backoff = 1.0
            if time.monotonic() >= next_cleanup:
                next_cleanup = time.monotonic() + CLEANUP_INTERVAL_SECONDS
                await self.cleanup_stale_groups()
            if resp:
                await self._dispatch_batch(resp)

    async def _dispatch_batch(self, resp) -> None:
        lanes: dict[tuple[str, str], list[dict]] = {}
        for _key, entries in resp:
            for entry_id, fields in entries:
                try:
                    envelope = json.loads(fields["envelope"])
                except Exception:
                    logger.exception("broadcast: unparseable envelope in entry %s", entry_id)
                    continue
                if envelope.get("origin") == self.group:
                    continue  # delivered locally when it was published
                lanes.setdefault(_lane(envelope), []).append(envelope)
        await asyncio.gather(*(self._run_lane(envs) for envs in lanes.values()))

    async def _run_lane(self, envelopes: list[dict]) -> None:
        for envelope in envelopes:
            try:
                await self._dispatch(envelope)
            except Exception:
                logger.exception("broadcast dispatch failed for %s", envelope.get("kind"))

    async def _dispatch(self, envelope: dict) -> None:
        kind = envelope.get("kind")
        if kind == "cache.invalidate":
            fn = self._invalidators.get(envelope.get("name", ""))
            if fn is None:
                return
            result = fn(envelope.get("key", ""))
            if asyncio.iscoroutine(result):
                await result
            return
        if kind not in ("ws", "room"):
            logger.warning("broadcast consumer: unknown envelope kind %r", kind)
            return

        from pocketpaw_ee.cloud.chat.schemas import WsOutbound

        frame = WsOutbound(type=envelope["type"], data=envelope.get("data", {}))
        manager = self._manager()
        if kind == "ws":
            await manager.broadcast_to_group(
                envelope["scope_id"], envelope.get("recipients", []), frame, relay=False
            )
        else:
            await manager.send_to_room(
                envelope["group_id"],
                frame,
                exclude_user=envelope.get("exclude_user"),
                relay=False,
            )


# --- module-level switch + singleton ------------------------------------------

_enabled = False
_channel: Channel | None = None


def configure(*, enabled: bool) -> None:
    """Turn cross-process broadcast on or off. Called by ``init_realtime``."""
    global _enabled
    _enabled = enabled


def is_enabled() -> bool:
    return _enabled


def active_channel() -> Channel | None:
    """This process's channel when broadcast is on, else ``None``."""
    global _channel
    if not _enabled:
        return None
    if _channel is None:
        _channel = Channel()
    return _channel


async def start() -> None:
    channel = active_channel()
    if channel is not None:
        await channel.start()


async def stop() -> None:
    if _channel is not None:
        await _channel.stop()


async def broadcast_invalidate(name: str, key: str, *, local: bool = True) -> None:
    """Invalidate ``name``/``key`` on this process (unless ``local=False``) and
    on every other web process. Remote delivery is asynchronous: the other
    processes clear their caches within one stream read."""
    if local:
        fn = _INVALIDATORS.get(name)
        if fn is not None:
            result = fn(key)
            if asyncio.iscoroutine(result):
                await result
    channel = active_channel()
    if channel is not None:
        await channel.publish_invalidate(name, key)


_pending: set[asyncio.Task] = set()


def broadcast_invalidate_soon(name: str, key: str) -> None:
    """Remote-only invalidation for sync callers. Schedules the publish on the
    running loop; a no-op when broadcast is off or no loop is running."""
    if active_channel() is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(broadcast_invalidate(name, key, local=False))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _reset_for_tests() -> None:
    global _enabled, _channel
    _enabled = False
    _channel = None
