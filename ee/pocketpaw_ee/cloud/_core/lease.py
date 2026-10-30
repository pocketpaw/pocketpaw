"""Single-runner leases for background loops when the web tier is N processes.

With ``POCKETPAW_REALTIME_BUS=redis-streams`` (the multi-worker switch, see
``_core/realtime/broadcast.py``) the web tier may be several processes, and a
scheduler started in each would run its work N times. ``leased(name, start,
stop)`` wraps a loop's start/stop pair so exactly one process runs it: a
``Lease`` takes ``SET lease:{name} owner NX PX ttl``, renews it every ttl/3
with a compare-and-pexpire Lua script, and calls ``start`` when it becomes the
holder and ``stop`` when it stops being one. On shutdown it stops the loop and
releases the key with a compare-and-delete, so a sibling takes over within one
renewal interval instead of waiting out the ttl. ``per_host=True`` keys the
lease by hostname, for work on per-host state (local disk, a local SQLite
file): one runner per host.

Without the switch ``leased(...).start()`` / ``.stop()`` call the loop's own
start/stop directly, as before. ``gate``/``may_run`` serve a loop that stays
running everywhere and checks the lease per tick instead (the run sweeper).

Invariants a reader must not break:

- ``held`` is a local monotonic deadline set from the last successful acquire
  or renewal, never a Redis read. That is what makes Redis being down stop the
  loop (after the deadline) rather than leave every process running it.
- The deadline sits a margin short of the key's ttl, and the renewal task
  wakes at the deadline, so the old holder stops before a contender can take
  the expired key. There is no fencing token: a tick already in flight when the
  lease is lost finishes.
- Only the owner can renew or release (both scripts compare the value first),
  so a holder that stalled past its ttl cannot delete its successor's lease.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable

from pocketpaw_ee.cloud._core.realtime import broadcast
from pocketpaw_ee.cloud._core.redis_client import get_redis

logger = logging.getLogger(__name__)

LEASE_PREFIX = "lease:"
DEFAULT_TTL_MS = 30_000
# The local deadline is this fraction of the ttl, leaving the rest as margin
# for clock drift and the renewal task's own latency.
_VALID_FRACTION = 0.8

_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

Callback = Callable[[], Awaitable[None]]


def multi_worker_enabled() -> bool:
    """True when the realtime bus is redis-streams, i.e. the web tier may be
    several processes and background loops need a lease."""
    return broadcast.is_enabled()


class Lease:
    """One contender for one named lease."""

    def __init__(
        self,
        name: str,
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
        per_host: bool = False,
        redis=None,
        prefix: str | None = None,
        on_acquired: Callback | None = None,
        on_lost: Callback | None = None,
    ) -> None:
        host = socket.gethostname()
        prefix = LEASE_PREFIX if prefix is None else prefix
        self.key = f"{prefix}{name}:{host}" if per_host else f"{prefix}{name}"
        self.owner = f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._ttl_ms = ttl_ms
        self._redis = redis
        self._on_acquired = on_acquired
        self._on_lost = on_lost
        self._valid_until = 0.0
        self._running = False  # whether on_acquired ran without a matching on_lost
        self._task: asyncio.Task | None = None

    def _r(self):
        return self._redis if self._redis is not None else get_redis()

    @property
    def held(self) -> bool:
        return time.monotonic() < self._valid_until

    async def try_acquire(self) -> bool:
        """Take the lease, or renew it if this contender already owns it."""
        started = time.monotonic()
        try:
            redis = self._r()
            ok = bool(await redis.set(self.key, self.owner, nx=True, px=self._ttl_ms))
            if not ok:
                ok = bool(await redis.eval(_RENEW_LUA, 1, self.key, self.owner, self._ttl_ms))
        except Exception:
            # Unreachable Redis: keep whatever validity is left and let the
            # deadline run out. Never extend it without a confirmed write.
            logger.warning("lease %s: acquire/renew failed", self.key, exc_info=True)
            return self.held
        if ok:
            self._valid_until = started + self._ttl_ms / 1000 * _VALID_FRACTION
        else:
            self._valid_until = 0.0  # someone else owns it
        return ok

    async def _sync_callbacks(self) -> None:
        if self.held and not self._running:
            self._running = True
            logger.info("lease %s acquired by %s", self.key, self.owner)
            if self._on_acquired is not None:
                try:
                    await self._on_acquired()
                except Exception:
                    logger.exception("lease %s: start callback failed", self.key)
        elif not self.held and self._running:
            self._running = False
            logger.warning("lease %s lost by %s; stopping its work", self.key, self.owner)
            if self._on_lost is not None:
                try:
                    await self._on_lost()
                except Exception:
                    logger.exception("lease %s: stop callback failed", self.key)

    async def tick(self) -> None:
        """One acquire/renew attempt, then start or stop the work to match."""
        await self.try_acquire()
        await self._sync_callbacks()

    async def _run(self) -> None:
        interval = self._ttl_ms / 1000 / 3
        while True:
            wait = interval
            if self.held:
                # Wake no later than the deadline so a failed renewal stops the
                # work before a contender can take the expired key.
                wait = min(interval, max(0.0, self._valid_until - time.monotonic()))
            await asyncio.sleep(wait)
            await self.tick()

    async def start(self) -> None:
        """First attempt inline (so a boot-time holder starts its work before
        this returns), then renew in the background."""
        if self._task is not None:
            return
        await self.tick()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop renewing, stop the work, and release the key if still ours."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._valid_until = 0.0
        await self._sync_callbacks()
        try:
            await self._r().eval(_RELEASE_LUA, 1, self.key, self.owner)
        except Exception:
            logger.debug("lease %s: release failed", self.key, exc_info=True)


class Leased:
    """A background loop's ``start``/``stop`` pair, run by one process at a time.

    Call ``start()``/``stop()`` from the lifespan hooks. The multi-worker switch
    is read when ``start()`` runs; off, the pair runs straight through.
    """

    def __init__(
        self,
        name: str,
        start: Callback,
        stop: Callback,
        *,
        per_host: bool = False,
        ttl_ms: int = DEFAULT_TTL_MS,
        redis=None,
    ) -> None:
        self.name = name
        self._start = start
        self._stop = stop
        self._per_host = per_host
        self._ttl_ms = ttl_ms
        self._redis = redis
        self.lease: Lease | None = None

    @property
    def held(self) -> bool:
        """Whether this process runs the loop (always, with the switch off)."""
        return self.lease is None or self.lease.held

    async def start(self) -> None:
        if not multi_worker_enabled():
            await self._start()
            return
        self.lease = Lease(
            self.name,
            ttl_ms=self._ttl_ms,
            per_host=self._per_host,
            redis=self._redis,
            on_acquired=self._start,
            on_lost=self._stop,
        )
        await self.lease.start()

    async def stop(self) -> None:
        lease, self.lease = self.lease, None
        if lease is None:
            await self._stop()
            return
        await lease.stop()


def leased(name: str, start: Callback, stop: Callback, **kw) -> Leased:
    """Wrap a loop's ``start``/``stop`` so one process runs it. See ``Leased``."""
    return Leased(name, start, stop, **kw)


def gate(name: str, *, per_host: bool = False, redis=None) -> Lease | None:
    """A lease with no callbacks, for a loop that checks ``held`` per tick.
    ``None`` when the multi-worker switch is off (the caller runs everything)."""
    if not multi_worker_enabled():
        return None
    return Lease(name, per_host=per_host, redis=redis)


def may_run(lease: Lease | None) -> bool:
    """Whether a loop gated by ``lease`` (from ``gate``) should do its work now."""
    return lease is None or lease.held
