"""Redis-Streams implementation of RunStreamTransport.

Key layout:
  run:{run_id}:events   XADD stream of SSE events (the resumable log)
  run:{run_id}:cancel   string flag; presence = cancellation requested

Each stream entry has fields {"event": <type>, "data": <json>}. The Redis
entry id ("<ms>-<seq>") is monotonic and is what a reconnecting client passes
back as the cursor.

Works with any Redis-protocol server: Redis, Dragonfly, Valkey.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis

from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent


def _events_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def _cancel_key(run_id: str) -> str:
    return f"run:{run_id}:cancel"


class RedisStreamTransport:
    """RunStreamTransport backed by Redis Streams."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def append_event(self, run_id: str, event: str, data: dict[str, Any]) -> str:
        return await self._redis.xadd(
            _events_key(run_id),
            {"event": event, "data": json.dumps(data)},
        )

    async def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]:
        """Yield events after the cursor, then block for live ones. Stops on
        a terminal event. Returns when ``block`` times out with no entries —
        the caller loops and emits a heartbeat between calls."""
        cursor = after
        while True:
            resp = await self._redis.xread({_events_key(run_id): cursor}, block=block_ms, count=64)
            if not resp:
                return
            _key, entries = resp[0]
            for entry_id, fields in entries:
                cursor = entry_id
                ev = StreamEvent(
                    entry_id=entry_id,
                    event=fields["event"],
                    data=json.loads(fields["data"]),
                )
                yield ev
                if ev.is_terminal:
                    return

    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None:
        await self._redis.expire(_events_key(run_id), ttl_seconds)
        await self._redis.expire(_cancel_key(run_id), ttl_seconds)

    async def request_cancel(self, run_id: str) -> None:
        await self._redis.set(_cancel_key(run_id), "1", ex=3600)

    async def is_cancelled(self, run_id: str) -> bool:
        return bool(await self._redis.exists(_cancel_key(run_id)))

    async def stream_exists(self, run_id: str) -> bool:
        return bool(await self._redis.exists(_events_key(run_id)))
