"""Redis-Streams implementation of RunStreamTransport.

Key layout:
  run:{run_id}:events   XADD stream of SSE events (resumable log)
  run:{run_id}:cancel   string flag; presence = cancellation requested
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import PurePath
from typing import Any

from redis.asyncio import Redis

from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent

logger = logging.getLogger(__name__)

# Types str() round-trips cleanly. Others coerce too (better than crashing the
# turn) but log so we notice junk like ``<MyObj at 0x…>`` reaching clients.
_KNOWN_STR_COERCIBLE = (datetime, date, PurePath, bytes)


def _encode_unknown(value: Any) -> str:
    if not isinstance(value, _KNOWN_STR_COERCIBLE):
        logger.warning(
            "redis_stream: lossy str() coercion of %s — fix producer or extend "
            "_KNOWN_STR_COERCIBLE",
            type(value).__name__,
        )
    return str(value)


def _events_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def _cancel_key(run_id: str) -> str:
    return f"run:{run_id}:cancel"


class RedisStreamTransport:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def append_event(self, run_id: str, event: str, data: dict[str, Any]) -> str:
        return await self._redis.xadd(
            _events_key(run_id),
            {"event": event, "data": json.dumps(data, default=_encode_unknown)},
        )

    async def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]:
        """Yield events then return on terminal event or ``block_ms`` timeout.
        Not infinite — callers re-invoke and emit heartbeats between calls."""
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
