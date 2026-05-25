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

# Types we know how to coerce losslessly via str() — datetimes round-trip to
# ISO, paths to their string form, bytes via decode-or-repr. Anything outside
# this set still coerces (better than crashing the whole turn) but logs a WARN
# so we notice degraded payloads instead of shipping ``<MyObj object at 0x…>``
# strings to clients silently.
_KNOWN_STR_COERCIBLE = (datetime, date, PurePath, bytes)


def _encode_unknown(value: Any) -> str:
    if not isinstance(value, _KNOWN_STR_COERCIBLE):
        logger.warning(
            "redis_stream: coercing non-primitive %s via str() — payload is "
            "lossy, fix the producer or extend _KNOWN_STR_COERCIBLE",
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
        # _encode_unknown coerces stragglers (bytes, datetime, Path, …) tool
        # results may carry. Without it, one un-serializable value would crash
        # ``execute_run`` and fail the whole turn. Unknown types still coerce
        # but emit a WARN — see ``_encode_unknown``.
        return await self._redis.xadd(
            _events_key(run_id),
            {"event": event, "data": json.dumps(data, default=_encode_unknown)},
        )

    async def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]:
        """Yield events from the run's stream, then return.

        Not an infinite tail: this iterator ends in three cases — terminal
        event yielded, ``xread`` returns empty after ``block_ms`` (no new
        events arrived), or the inner ``xread`` loop exhausts itself.
        Callers (the router's SSE loop) re-call this between iterations and
        emit heartbeats during the gaps.
        """
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
