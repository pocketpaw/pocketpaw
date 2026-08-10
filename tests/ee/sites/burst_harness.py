# tests/ee/sites/burst_harness.py — the site-build lane's burst / concurrency harness.
#
# Created 2026-08-11 (D5). The captain's ruling made EVERY publish a Daytona sandbox, so
# the single-flight guard in ``sites/build_state.py`` moved onto the critical path for
# both cost and correctness — and nothing measured it under contention. This module is
# the fake lane that lets a burst be measured without spending a sandbox.
#
# WHY A FAKE LANE RATHER THAN A LIVE BURST. A live burst spends real sandboxes and real
# money, and it is the captain's call to make deliberately, not a side effect of running
# the test suite. Everything here is in-process: the "sandbox" is a recorded enqueue, the
# clock is injected, and no code path in this file can reach the Daytona API or Redis. A
# harness that needs ``DAYTONA_API_KEY`` to run is the wrong harness.
#
# WHAT MAKES THIS CHEAP. ``build_state`` is pure — no Beanie, no I/O, and its functions
# take ``now=`` — so a burst is a loop over an injected clock rather than a wait. That is
# deliberate on both sides: a harness that takes minutes is a harness nobody runs.
#
# THE TWO CONCURRENCY MODELS, and why both are here. The guard is a READ-then-WRITE, not
# a compare-and-swap, so what it guarantees depends entirely on whether the stamp lands
# before a sibling reads:
#
#   * SHARED-DOC (``burst_on_one_doc``) — N publishes racing on ONE in-memory row, which
#     is what a single process re-entering publish looks like. The stamp is visible to
#     every later reader as soon as it lands.
#   * SEPARATE-DOC (``burst_on_reloaded_docs``) — N publishes that each LOAD their own
#     copy of the row first, which is what N concurrent HTTP requests actually look like.
#     Each reader has its own snapshot, so a stamp written by one is invisible to a
#     sibling that read before it.
#
# The second is the honest model of production and the harder case. Keeping both is the
# point: the difference between them is the size of the guard's race window, and a
# harness that only ran the friendly model would report a guarantee the lane does not
# have. See docs/runbooks/2026-08-11-site-build-burst-harness.md for what this can and
# cannot tell us.

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

BUILD_TIMEOUT = 600
"""The engine budget both engines currently resolve to (``daytona_build``'s floor).
Hard-coded rather than resolved so a burst's arithmetic stays readable, and so an env
var on the machine running the tests cannot move the window under the assertions."""


# ---------------------------------------------------------------------------
# The fake build runner
# ---------------------------------------------------------------------------


class RecordingPool:
    """An arq pool that RECORDS an enqueue instead of performing one.

    Each recorded call is one sandbox that a real lane would have created, so
    ``len(pool.calls)`` is the harness's cost meter: the number this reports for a burst
    of N publishes of one site is the number of Daytona bills that burst would produce.
    Two is the failure the single-flight guard exists to prevent.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(
        self, function: str, *args: Any, _job_id: str | None = None, **kwargs: Any
    ) -> object:
        self.calls.append({"function": function, "args": args, "job_id": _job_id, "kwargs": kwargs})
        return object()

    @property
    def sandboxes(self) -> int:
        """How many sandboxes this burst would have opened."""
        return len(self.calls)

    @property
    def job_ids(self) -> list[str | None]:
        return [call["job_id"] for call in self.calls]


class YieldingPool(RecordingPool):
    """A pool that hands control back to the loop BEFORE recording.

    Widens the interleaving window on purpose. A real ``enqueue_job`` is a Redis round
    trip, so the loop is free to run a sibling publish mid-enqueue; a fake that records
    synchronously hides any ordering bug that window exposes.
    """

    async def enqueue_job(
        self, function: str, *args: Any, _job_id: str | None = None, **kwargs: Any
    ) -> object:
        await asyncio.sleep(0)
        return await super().enqueue_job(function, *args, _job_id=_job_id, **kwargs)


# ---------------------------------------------------------------------------
# The fake row
# ---------------------------------------------------------------------------


@dataclass
class FakeSite:
    """A Site-shaped row whose writes yield to the loop, like a DB round trip does.

    ``set`` is the load-bearing detail. A fake that applied a write synchronously would
    make every burst pass, because no sibling could ever observe the pre-write state —
    the harness would be measuring its own fake instead of the guard. Yielding first
    reproduces the real gap between a gate's read and its stamp.

    Every write is appended to ``transitions``, which is what lets a test assert that
    ``queued`` and ``building`` stayed DISTINGUISHABLE under load rather than merely
    that the row ended up somewhere plausible.
    """

    workspace: str = "ws-burst"
    id: str = "site-burst"
    build_status: str = "none"
    build_started_at: datetime | None = None
    build_job_id: str | None = None
    build_reason: str | None = None
    transitions: list[tuple[str, str | None]] = field(default_factory=list)

    async def set(self, values: dict[str, Any]) -> None:
        await asyncio.sleep(0)  # a real write is a round trip; a sibling may run here
        for key, value in values.items():
            setattr(self, key, value)
        if "build_status" in values:
            self.transitions.append((values["build_status"], values.get("build_reason")))

    def snapshot(self) -> FakeSite:
        """An independent copy of the row as it reads RIGHT NOW.

        This is what "each request loads its own doc" means: a snapshot taken before a
        sibling's stamp lands never sees that stamp, however the loop interleaves after.
        """
        return FakeSite(
            workspace=self.workspace,
            id=self.id,
            build_status=self.build_status,
            build_started_at=self.build_started_at,
            build_job_id=self.build_job_id,
            build_reason=self.build_reason,
            transitions=self.transitions,  # shared on purpose: one row, one history
        )


def aged_site(status: str, *, age_seconds: float | None, **kwargs: Any) -> FakeSite:
    """A row in ``status`` whose stamp is ``age_seconds`` old (``None`` = no stamp)."""
    stamp = None if age_seconds is None else datetime.now(UTC) - timedelta(seconds=age_seconds)
    return FakeSite(build_status=status, build_started_at=stamp, **kwargs)


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


class Clock:
    """An injected clock. The guard's functions all take ``now=``, so a burst that spans
    an hour of window arithmetic runs in microseconds and lands on exact boundaries —
    which a sleeping harness cannot do, and which is where the interesting bugs are."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now

    def ago(self, **delta: float) -> datetime:
        return self.now - timedelta(**delta)


# ---------------------------------------------------------------------------
# The burst drivers
# ---------------------------------------------------------------------------


@dataclass
class BurstReport:
    """What one burst of N publishes did.

    ``sandboxes`` is the cost line and the assertion that matters: for N publishes of one
    site it must be 1. ``refused`` counts the publishes the guard turned away — a
    ``None`` return from ``enqueue_site_build``, which is the lane's "a build is already
    in flight" answer, not an error.
    """

    results: list[Any]
    sandboxes: int
    transitions: list[tuple[str, str | None]]

    @property
    def job_ids(self) -> list[str]:
        return [r for r in self.results if isinstance(r, str)]

    @property
    def refused(self) -> int:
        return sum(1 for r in self.results if r is None)

    @property
    def errors(self) -> list[BaseException]:
        return [r for r in self.results if isinstance(r, BaseException)]


async def _gather(coros: list[Any]) -> list[Any]:
    return list(await asyncio.gather(*coros, return_exceptions=True))


async def burst_on_one_doc(
    site: FakeSite,
    n: int,
    *,
    pool: RecordingPool | None = None,
    engine: str = "react",
) -> BurstReport:
    """N concurrent publishes racing on ONE row object.

    The friendly model: a stamp is visible to every reader that has not yet gated. This
    is a single process re-entering publish, not two HTTP requests.
    """
    from pocketpaw_ee.sites import build_job

    pool = pool or RecordingPool()
    results = await _gather(
        [
            build_job.enqueue_site_build(
                site,
                engine=engine,
                generator_input=_input(),
                timeout_seconds=BUILD_TIMEOUT,
                _pool_override=pool,
            )
            for _ in range(n)
        ]
    )
    return BurstReport(results=results, sandboxes=pool.sandboxes, transitions=site.transitions)


async def burst_on_reloaded_docs(
    site: FakeSite,
    n: int,
    *,
    pool: RecordingPool | None = None,
    engine: str = "react",
) -> BurstReport:
    """N concurrent publishes that each LOAD their own copy of the row first.

    The production model: N HTTP requests, each with its own snapshot of the row. Every
    snapshot is taken before any stamp lands, which is exactly the window a read-then-
    write guard leaves open and a compare-and-swap would close.
    """
    from pocketpaw_ee.sites import build_job

    pool = pool or RecordingPool()
    snapshots = [site.snapshot() for _ in range(n)]
    results = await _gather(
        [
            build_job.enqueue_site_build(
                snap,
                engine=engine,
                generator_input=_input(),
                timeout_seconds=BUILD_TIMEOUT,
                _pool_override=pool,
            )
            for snap in snapshots
        ]
    )
    return BurstReport(results=results, sandboxes=pool.sandboxes, transitions=site.transitions)


async def burst_on_distinct_sites(
    n: int,
    *,
    pool: RecordingPool | None = None,
    engine: str = "react",
) -> BurstReport:
    """N concurrent publishes of N DIFFERENT sites.

    The control case, and the one that must NOT be throttled: the guard is per-site, so
    N distinct sites are N legitimate builds. A guard that collapsed them would be a
    global lock wearing a single-flight costume, and it would serialise every customer
    behind whoever published first.
    """
    from pocketpaw_ee.sites import build_job

    pool = pool or RecordingPool()
    sites = [FakeSite(id=f"site-{i}") for i in range(n)]
    results = await _gather(
        [
            build_job.enqueue_site_build(
                s,
                engine=engine,
                generator_input=_input(),
                timeout_seconds=BUILD_TIMEOUT,
                _pool_override=pool,
            )
            for s in sites
        ]
    )
    transitions = [t for s in sites for t in s.transitions]
    return BurstReport(results=results, sandboxes=pool.sandboxes, transitions=transitions)


def _input() -> dict[str, Any]:
    """The smallest generator payload the enqueue will accept. Scrubbed by the lane on
    the way in, so nothing here needs to be realistic."""
    return {"siteId": "site-burst", "spec": {"sections": []}}


__all__ = [
    "BUILD_TIMEOUT",
    "BurstReport",
    "Clock",
    "FakeSite",
    "RecordingPool",
    "YieldingPool",
    "aged_site",
    "burst_on_distinct_sites",
    "burst_on_one_doc",
    "burst_on_reloaded_docs",
]
