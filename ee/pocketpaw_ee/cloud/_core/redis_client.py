"""Process-wide Redis client singleton. URL from ``POCKETPAW_REDIS_URL``.

Changes: 2026-09-04 (fix/pool-and-body-ceilings, backend-perf H6) — the client
was built with no pool ceiling and no connect timeout. redis-py's async
``ConnectionPool`` defaults ``max_connections`` to an effectively unbounded
value, which matters here more than it would in most apps: every open SSE run
stream holds a connection parked in ``XREAD BLOCK 15000`` for as long as the
client stays subscribed. Connections were therefore a direct function of open
browser tabs, with no ceiling and no early signal on the way to exhausting the
server's own ``maxclients``.
"""

from __future__ import annotations

import logging
import os

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_client: Redis | None = None

# Ceiling on connections this process will open.
#
# Sized against the thing that actually consumes them: one parked connection
# per live SSE subscription. 128 is comfortably above the concurrent-stream
# count a single-process deploy can serve (arq's cluster-wide job ceiling is
# 10 by default, and a stream outlives its run only by the grace window) while
# still being a number rather than "however many the tab count implies".
#
# What exhaustion looks like now: redis-py raises "Too many connections", which
# surfaces as a fast, attributable error. Before, the pool kept opening
# sockets until the Redis server refused them, and the failure surfaced
# somewhere else entirely.
_DEFAULT_MAX_CONNECTIONS = 128

# Bound on establishing a connection. Deliberately NOT ``socket_timeout``:
# that one bounds every read, and the run-stream reader parks in
# ``XREAD BLOCK 15000`` by design, so a read timeout would sever healthy
# streams on a 15-second cycle.
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


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _reset_for_tests() -> None:
    global _client
    _client = None
