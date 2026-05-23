"""Process-wide Redis client singleton for cloud chat runs.

A future RedisBus (realtime) may share this. The connection URL comes from
``POCKETPAW_REDIS_URL`` (e.g. ``redis://redis:6379/0``).
"""

from __future__ import annotations

import os

from redis.asyncio import Redis

_client: Redis | None = None


def get_redis() -> Redis:
    """Return the shared async Redis client, creating it on first use."""
    global _client
    if _client is None:
        url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("POCKETPAW_REDIS_URL is not set — resumable chat runs need Redis.")
        # decode_responses=False: Redis Stream entry IDs and our JSON payloads
        # are handled as bytes/str explicitly in redis_stream.py.
        _client = Redis.from_url(url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _reset_for_tests() -> None:
    """Drop the cached client so a test can re-create it. Test-only."""
    global _client
    _client = None
