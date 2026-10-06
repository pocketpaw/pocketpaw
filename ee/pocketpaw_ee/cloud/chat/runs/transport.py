"""Transport abstraction for chat-run events. Backend selected by
``POCKETPAW_CLOUD_STREAM_TRANSPORT``. ``sse_tail`` is the one stream -> SSE
loop, shared by the chat run stream and the Belt run feed stream."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

TERMINAL_EVENTS = {"stream_end", "error", "interrupted"}


@dataclass(frozen=True)
class StreamEvent:
    entry_id: str  # opaque cursor
    event: str
    data: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.event in TERMINAL_EVENTS


@runtime_checkable
class RunStreamTransport(Protocol):
    async def append_event(self, run_id: str, event: str, data: dict[str, Any]) -> str: ...

    def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]: ...

    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None: ...
    async def request_cancel(self, run_id: str) -> None: ...
    async def is_cancelled(self, run_id: str) -> bool: ...
    async def stream_exists(self, run_id: str) -> bool: ...


def sse_frame(entry_id: str, event: str, data: dict[str, Any]) -> bytes:
    """One SSE frame; ``id`` is the stream cursor a client resumes ``after``."""
    return f"id: {entry_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def sse_tail(
    transport: RunStreamTransport, run_id: str, after: str, deadline: float
) -> AsyncIterator[bytes]:
    """A run stream as SSE from ``after`` until its terminal event, with a
    ``: ping`` heartbeat between blocking reads so proxies keep the connection
    open. At ``deadline`` (``time.monotonic``) it ends with a real ``error``
    frame (``run.stream_timeout``): without it the loop never exits on its own,
    since a stream whose writer died before its terminal frame heartbeats
    forever, holding a blocked Redis connection and a live task, and a client
    disconnect is only noticed when a ``yield`` fails. ``error`` is terminal,
    so the client stops waiting and offers a retry."""
    cursor = after
    while True:
        saw_terminal = False
        async for ev in transport.read_events(run_id, after=cursor, block_ms=15000):
            cursor = ev.entry_id
            yield sse_frame(ev.entry_id, ev.event, ev.data)
            if ev.is_terminal:
                saw_terminal = True
        if saw_terminal:
            return
        if time.monotonic() >= deadline:
            logger.warning(
                "run stream exceeded max lifetime; closing run_id=%s cursor=%s", run_id, cursor
            )
            yield sse_frame(
                cursor,
                "error",
                {
                    "code": "run.stream_timeout",
                    "message": "This run's stream was open too long and was closed.",
                },
            )
            return
        yield b": ping\n\n"


_transport: RunStreamTransport | None = None


def get_stream_transport() -> RunStreamTransport:
    global _transport
    if _transport is None:
        backend = os.environ.get("POCKETPAW_CLOUD_STREAM_TRANSPORT", "").strip().lower()
        if not backend:
            # Auto: Redis if URL is set, else in-memory (Tier 0 dev) with a
            # loud WARN so prod operators notice a missing env var.
            if os.environ.get("POCKETPAW_REDIS_URL", "").strip():
                backend = "redis"
            else:
                backend = "memory"
                logger.warning(
                    "POCKETPAW_REDIS_URL unset — using in-memory stream transport. "
                    "Runs do NOT survive process restart and Tier 2 worker is "
                    "unavailable. Set POCKETPAW_REDIS_URL for production."
                )
        if backend == "memory":
            from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

            _transport = InMemoryStreamTransport()
        elif backend == "redis":
            from pocketpaw_ee.cloud._core.redis_client import get_blocking_redis, get_redis
            from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

            _transport = RedisStreamTransport(get_redis(), blocking_redis=get_blocking_redis())
        else:
            raise RuntimeError(f"unknown POCKETPAW_CLOUD_STREAM_TRANSPORT={backend!r}")
    return _transport


def _reset_for_tests() -> None:
    global _transport
    _transport = None
