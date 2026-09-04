# ee/pocketpaw_ee/cloud/_core/realtime/fanout.py
# Created: 2026-09-04 (fix/unblock-event-loop, backend-perf H1) — bounded
# concurrency for realtime delivery.
#
# Why this exists: every realtime fan-out in the cloud package was a `for`
# loop with one `await` per recipient, running INLINE in the emitting HTTP
# request. Delivery cost was therefore the SUM over recipients, not the max.
#
# The codebase had already found the failure mode and half-fixed it:
# `ws.SEND_TIMEOUT_SECONDS = 5.0` exists precisely so "one stuck socket would
# stall delivery to every other member and the sender's own request". But a
# per-send timeout bounds ONE send. It does nothing about the loop around it.
# A 200-member workspace with ten backgrounded mobile tabs cost the sender's
# POST up to 50 seconds, serially, with the 5s timeout working exactly as
# designed the whole time.
#
# Fanning out concurrently makes the cost max-per-recipient instead. The bound
# matters as much as the concurrency: an unbounded `gather` over a large
# workspace would mint one task per member, which trades a latency problem for
# a memory-and-scheduler one.

"""Bounded-concurrency fan-out helper for realtime delivery.

Deliberately import-free beyond the stdlib. ``_core.realtime.bus`` already has
to import ``chat.schemas`` lazily to dodge a cycle, so anything both it and
``chat.ws`` depend on has to be a leaf.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, TypeVar

T = TypeVar("T")
R = TypeVar("R")

#: Maximum sends in flight for a single fan-out.
#:
#: Chosen to be far above a normal workspace's online membership (so the common
#: case is fully parallel and costs one round of sends) while still capping the
#: task count for a pathologically large audience. Delivery to a 1000-member
#: workspace is then 32 concurrent sends deep rather than 1000 tasks wide, and
#: worst-case latency is ceil(1000/32) * SEND_TIMEOUT rather than 1000 *
#: SEND_TIMEOUT.
FANOUT_CONCURRENCY: Final = 32


async def map_bounded(
    items: Sequence[T],
    fn: Callable[[T], Awaitable[R]],
    *,
    limit: int = FANOUT_CONCURRENCY,
) -> list[R]:
    """Await ``fn`` over ``items`` with at most ``limit`` in flight.

    Results come back positionally, so a caller can zip them against ``items``
    to attribute each outcome.

    Exceptions are NOT captured here. Every caller on the fan-out path already
    contains its own failures (``_send_bounded`` returns a bool and swallows;
    the bus logs per recipient), and swallowing here as well would hide a real
    bug in a new caller. It also keeps cancellation honest: ``gather`` with
    ``return_exceptions=True`` turns a child's ``CancelledError`` into a
    RESULT, so a cancelled request would look like a successful fan-out.
    """
    if not items:
        return []
    if len(items) == 1:
        # Skip the semaphore and the task machinery for the overwhelmingly
        # common case of one recipient — that is the single-socket user, and
        # it is the whole steady-state load of a small workspace.
        return [await fn(items[0])]

    sem = asyncio.Semaphore(limit)

    async def _one(item: T) -> R:
        async with sem:
            return await fn(item)

    return list(await asyncio.gather(*(_one(item) for item in items)))


__all__ = ["FANOUT_CONCURRENCY", "map_bounded"]
