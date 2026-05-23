"""Redis-Streams implementation of RunStreamTransport.

Key layout:
  run:{run_id}:events   XADD stream of SSE events (resumable log)
  run:{run_id}:cancel   string flag; presence = cancellation requested
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
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def append_event(self, run_id: str, event: str, data: dict[str, Any]) -> str:
        # ``default=str`` coerces stragglers (bytes, datetime, Path, custom
        # objects) tool results may carry. Without this, one un-serializable
        # value would crash ``execute_run`` and fail the whole turn.
        return await self._redis.xadd(
            _events_key(run_id),
            {"event": event, "data": json.dumps(data, default=str)},
        )

    async def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]:
        # Returns on terminal event OR block timeout — caller loops + emits heartbeat.
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
