"""Process-wide Redis clients. URL from ``POCKETPAW_REDIS_URL``.

Changes (2026-10-01, CN-4): added ``get_arq_pool()`` / ``close_arq_pool()``, the
one process-wide arq enqueue pool. It replaces four copied lazy getters (chat
runs, workspace jobs, site build, site delete), three of which were never closed
on shutdown. ``CloudLifecycleHook.on_shutdown`` now closes it.

Two clients, two pools, on purpose:

- ``get_redis()`` is the shared client for short commands: ws tickets, cancel
  keys, session revocation, rate limits, arq enqueue, stream appends. Its pool
  is non-blocking and capped by ``POCKETPAW_REDIS_MAX_CONNECTIONS`` (default
  128); exhaustion raises "Too many connections" at once.
- ``get_blocking_redis()`` is only for commands that park a connection in
  ``XREAD``/``XREADGROUP BLOCK`` (the run-stream SSE reader, the xproc
  consumer). Each open SSE stream holds one of these for up to 15s at a time,
  so they get their own ``BlockingConnectionPool`` capped by
  ``POCKETPAW_REDIS_STREAM_MAX_CONNECTIONS`` (default 512). An over-limit
  reader waits briefly for a slot and then fails with a ConnectionError; the
  shared pool never sees the pressure.

Invariant: any new ``block=`` read goes through ``get_blocking_redis()``.
Putting one on the shared client lets open browser tabs starve every other
Redis user in the process.
"""

from __future__ import annotations

import asyncio
import logging
import os

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from redis.asyncio import BlockingConnectionPool, Redis

logger = logging.getLogger(__name__)

_client: Redis | None = None
_blocking_client: Redis | None = None
_arq_pool: ArqRedis | None = None
_arq_pool_lock = asyncio.Lock()

# Ceiling on the shared pool. Blocking stream reads live on their own pool, so
# this only has to cover short request-scoped commands. Exhaustion raises
# "Too many connections" immediately rather than opening sockets until the
# Redis server's own maxclients refuses them.
_DEFAULT_MAX_CONNECTIONS = 128

# Ceiling on the blocking-read pool: roughly one connection per concurrently
# open SSE stream in this process, plus the xproc consumer.
_DEFAULT_STREAM_MAX_CONNECTIONS = 512

# How long a stream reader waits for a free blocking connection before failing.
# Short: the SSE loop reports the error to the client, which reconnects.
_BLOCKING_POOL_WAIT_SECONDS = 5.0

# Read timeout for the blocking client. Must stay comfortably above the longest
# ``block=`` in use (15s for both the SSE reader and xproc), or healthy parked
# reads get severed. A dead TCP flow is detected within this bound instead of
# hanging forever.
_BLOCKING_SOCKET_TIMEOUT_SECONDS = 30.0

# Bound on establishing a connection. The shared client deliberately sets no
# ``socket_timeout``: that bounds every read, and a read timeout there would
# sever anything that blocks. Only the blocking client, whose block window is
# known, sets one.
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

# Ping an idle pooled connection before reuse if it has been sitting this long.
# A NAT or proxy that silently drops an idle TCP flow otherwise hands back a
# dead connection on the next checkout, which fails the caller rather than the
# health check.
_DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 30


def _int_env(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on anything else.

    Fail-soft in the same shape as the other knobs in this codebase: a typo
    must not remove a ceiling, and must not crash boot either.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int; using default %d", name, raw, default)
        return default
    if val <= 0:
        logger.warning("%s=%d is not positive; using default %d", name, val, default)
        return default
    return val


def get_redis() -> Redis:
    global _client
    if _client is None:
        url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("POCKETPAW_REDIS_URL is not set — resumable chat runs need Redis.")
        _client = Redis.from_url(
            url,
            decode_responses=True,
            max_connections=_int_env("POCKETPAW_REDIS_MAX_CONNECTIONS", _DEFAULT_MAX_CONNECTIONS),
            socket_connect_timeout=_DEFAULT_CONNECT_TIMEOUT_SECONDS,
            health_check_interval=_DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
            retry_on_timeout=True,
        )
    return _client


def get_blocking_redis() -> Redis:
    """Client for ``XREAD``/``XREADGROUP`` with ``block=``. Nothing else."""
    global _blocking_client
    if _blocking_client is None:
        url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("POCKETPAW_REDIS_URL is not set — resumable chat runs need Redis.")
        pool = BlockingConnectionPool.from_url(
            url,
            decode_responses=True,
            max_connections=_int_env(
                "POCKETPAW_REDIS_STREAM_MAX_CONNECTIONS", _DEFAULT_STREAM_MAX_CONNECTIONS
            ),
            timeout=_BLOCKING_POOL_WAIT_SECONDS,
            socket_connect_timeout=_DEFAULT_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=_BLOCKING_SOCKET_TIMEOUT_SECONDS,
            health_check_interval=_DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
        )
        _blocking_client = Redis.from_pool(pool)
    return _blocking_client


async def close_redis() -> None:
    global _client, _blocking_client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _blocking_client is not None:
        await _blocking_client.aclose()
        _blocking_client = None


async def get_arq_pool() -> ArqRedis:
    """The process's arq pool, for ``enqueue_job`` only (every queue shares it;
    ``_queue_name`` picks the queue). Double-checked lock so concurrent first
    enqueues don't leak a second pool."""
    global _arq_pool
    if _arq_pool is None:
        async with _arq_pool_lock:
            if _arq_pool is None:
                url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
                if not url:
                    raise RuntimeError(
                        "POCKETPAW_REDIS_URL is not set — arq job queues need Redis."
                    )
                _arq_pool = await create_pool(RedisSettings.from_dsn(url))
    return _arq_pool


async def close_arq_pool() -> None:
    """Close the arq pool on web-process shutdown. No-op if never built; a
    failing aclose is swallowed because shutdown paths can't afford to raise."""
    global _arq_pool
    pool = _arq_pool
    _arq_pool = None
    if pool is None:
        return
    try:
        await pool.aclose()
    except Exception:
        logger.debug("arq pool aclose failed during shutdown", exc_info=True)


def _reset_for_tests() -> None:
    global _client, _blocking_client, _arq_pool
    _client = None
    _blocking_client = None
    _arq_pool = None
