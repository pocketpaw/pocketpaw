"""Process-wide Redis client singleton. URL from ``POCKETPAW_REDIS_URL``."""

from __future__ import annotations

import os

from redis.asyncio import Redis

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("POCKETPAW_REDIS_URL is not set — resumable chat runs need Redis.")
        _client = Redis.from_url(url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _reset_for_tests() -> None:
    global _client
    _client = None
