"""In-process stream transport — Tier 0 (dev) fallback when Redis is unset.

Ephemeral: buffers live in this process only, lost on restart, invisible to
other replicas. Only sound when the executor is also in-process. See
``transport.get_stream_transport`` for the selection logic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent


@dataclass
class _RunBuffer:
    entries: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    seq: int = 0
    ttl_task: asyncio.Task | None = None

    def next_id(self) -> str:
        self.seq += 1
        # Zero-pad so lex order matches arrival order — matches the Redis
        # streams entry-id ordering the router treats as opaque.
        return f"{self.seq:013d}-0"


class InMemoryStreamTransport:
    def __init__(self) -> None:
        self._buffers: dict[str, _RunBuffer] = {}

    def _buf(self, run_id: str) -> _RunBuffer:
        b = self._buffers.get(run_id)
        if b is None:
            b = _RunBuffer()
            self._buffers[run_id] = b
        return b

    async def append_event(self, run_id: str, event: str, data: dict[str, Any]) -> str:
        b = self._buf(run_id)
        entry_id = b.next_id()
        b.entries.append((entry_id, event, data))
        b.event.set()
        return entry_id

    async def read_events(
        self, run_id: str, *, after: str = "0", block_ms: int = 15000
    ) -> AsyncIterator[StreamEvent]:
        b = self._buf(run_id)
        # Translate the opaque cursor into a list index; unknown id = caught up.
        cursor_index = 0
        if after and after != "0":
            cursor_index = len(b.entries)
            for i, (eid, _, _) in enumerate(b.entries):
                if eid == after:
                    cursor_index = i + 1
                    break

        deadline = asyncio.get_event_loop().time() + block_ms / 1000

        while True:
            while cursor_index < len(b.entries):
                eid, ev_name, data = b.entries[cursor_index]
                cursor_index += 1
                ev = StreamEvent(entry_id=eid, event=ev_name, data=data)
                yield ev
                if ev.is_terminal:
                    return

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return
            # Clear-before-check avoids losing an append that lands between
            # the drain loop and the wait.
            b.event.clear()
            if cursor_index < len(b.entries):
                continue
            try:
                await asyncio.wait_for(b.event.wait(), timeout=remaining)
            except TimeoutError:
                return

    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None:
        b = self._buffers.get(run_id)
        if b is None:
            return
        if b.ttl_task is not None and not b.ttl_task.done():
            b.ttl_task.cancel()

        async def _expire() -> None:
            try:
                await asyncio.sleep(ttl_seconds)
            except asyncio.CancelledError:
                return
            self._buffers.pop(run_id, None)

        b.ttl_task = asyncio.create_task(_expire())

    async def request_cancel(self, run_id: str) -> None:
        self._buf(run_id).cancelled = True

    async def is_cancelled(self, run_id: str) -> bool:
        b = self._buffers.get(run_id)
        return bool(b and b.cancelled)

    async def stream_exists(self, run_id: str) -> bool:
        b = self._buffers.get(run_id)
        return bool(b and b.entries)
