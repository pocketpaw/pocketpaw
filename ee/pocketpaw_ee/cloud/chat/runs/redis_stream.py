"""Redis-Streams implementation of RunStreamTransport.

Key layout:
  run:{run_id}:events   XADD stream of SSE events (resumable log)
  run:{run_id}:cancel   string flag; presence = cancellation requested

Two clients: appends and flags go through the shared client; ``read_events``
parks in ``XREAD BLOCK`` and so goes through the blocking client (see
``_core/redis_client.py``), keeping open SSE tabs off the shared pool.

TTL: every append sets ``EXPIRE ... NX`` to ``initial_stream_ttl()`` (run
timeout + grace + retention), so a stream whose worker died before its terminal
write still expires. NX means it only lands on a key with no TTL: a longer TTL
set at enqueue, or the shorter one terminal paths set via ``set_ttl``, is never
overwritten by a later append.

No MAXLEN: a resuming reader replays from "0", and trimming would silently drop
the start of a long run's transcript. Size is bounded by the run's own timeout
and the TTL above.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import PurePath
from typing import Any

from redis.asyncio import Redis

from pocketpaw_ee.cloud.chat.runs.domain import stream_max_lifetime_seconds
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


_DEFAULT_RUN_STREAM_TTL_SECONDS = 3600
_expire_nx_warned = False


def _terminal_stream_ttl() -> int:
    """``POCKETPAW_CLOUD_RUN_STREAM_TTL``, the retention after a run ends.
    Same env var and default as ``run_core._stream_ttl``; fail-soft here since
    a typo must not break every append."""
    raw = os.environ.get("POCKETPAW_CLOUD_RUN_STREAM_TTL", "").strip()
    try:
        val = int(raw) if raw else _DEFAULT_RUN_STREAM_TTL_SECONDS
    except ValueError:
        return _DEFAULT_RUN_STREAM_TTL_SECONDS
    return val if val > 0 else _DEFAULT_RUN_STREAM_TTL_SECONDS


def initial_stream_ttl() -> int:
    """TTL a stream gets at its first append: long enough for the whole run
    plus the post-run retention window, so it only bites an orphaned stream."""
    return stream_max_lifetime_seconds() + _terminal_stream_ttl()


def _events_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def _cancel_key(run_id: str) -> str:
    return f"run:{run_id}:cancel"


class RedisStreamTransport:
    def __init__(self, redis: Redis, *, blocking_redis: Redis | None = None) -> None:
        self._redis = redis
        self._blocking = blocking_redis if blocking_redis is not None else redis

    async def append_event(self, run_id: str, event: str, data: dict[str, Any]) -> str:
        key = _events_key(run_id)
        payload = {"event": event, "data": json.dumps(data, default=_encode_unknown)}
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.xadd(key, payload)
            pipe.expire(key, initial_stream_ttl(), nx=True)
            entry_id, expired = await pipe.execute(raise_on_error=False)
        if isinstance(entry_id, Exception):
            raise entry_id
        if isinstance(expired, Exception):
            # e.g. Redis < 7 has no EXPIRE NX. The event is written; only the
            # orphan-stream safety net is missing. Warn once, not per chunk.
            global _expire_nx_warned
            if not _expire_nx_warned:
                _expire_nx_warned = True
                logger.warning("redis_stream: EXPIRE NX failed on %s: %s", key, expired)
        return entry_id

    async def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]:
        """Yield events then return on terminal event or ``block_ms`` timeout.
        Not infinite — callers re-invoke and emit heartbeats between calls."""
        cursor = after
        while True:
            resp = await self._blocking.xread(
                {_events_key(run_id): cursor}, block=block_ms, count=64
            )
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
