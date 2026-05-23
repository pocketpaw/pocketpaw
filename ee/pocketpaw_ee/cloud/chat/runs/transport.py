"""Transport abstraction for chat-run events. Backend selected by
``POCKETPAW_CLOUD_STREAM_TRANSPORT``."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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


_transport: RunStreamTransport | None = None


def get_stream_transport() -> RunStreamTransport:
    global _transport
    if _transport is None:
        backend = os.environ.get("POCKETPAW_CLOUD_STREAM_TRANSPORT", "redis").lower()
        if backend == "redis":
            from pocketpaw_ee.cloud._core.redis_client import get_redis
            from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

            _transport = RedisStreamTransport(get_redis())
        else:
            raise RuntimeError(f"unknown POCKETPAW_CLOUD_STREAM_TRANSPORT={backend!r}")
    return _transport


def _reset_for_tests() -> None:
    global _transport
    _transport = None
