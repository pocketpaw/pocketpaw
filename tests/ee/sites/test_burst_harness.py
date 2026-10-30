# tests/ee/sites/test_burst_harness.py — the site-build lane under a burst.
#
# Created 2026-08-11 (D5). Every publish is now a Daytona sandbox, which puts the
# single-flight guard on the critical path for MONEY as well as correctness, and nothing
# measured it under contention. These run the guard against a faked lane
# (``burst_harness.py``): no Daytona, no Redis, no sandbox, an injected clock, and every
# "sandbox" is a recorded enqueue.
#
# WHAT THIS FOUND, and it is the reason the file exists rather than a footnote:
# ``enqueue_site_build`` gated with a READ (``should_enqueue``) and then stamped with a
# WRITE (``mark_build_queued``), with an await in between and no compare-and-swap. Every
# publish arriving inside that window read the pre-stamp row, passed the gate, and opened
# its own sandbox. At N=8 that was EIGHT sandboxes for one site. The guard's own logic was
# never wrong — a burst arriving after the stamp lands is refused in full — so the defect
# was missing atomicity at the row, not the state machine.
#
# Edited 2026-08-11 (fix/atomic-queued-stamp): the assertions in
# ``TestSingleFlightUnderContention`` are FLIPPED from the pinned defect numbers to the
# fixed ones (one sandbox, N-1 refusals), which is what they were written to be the
# acceptance test for. ``claim_build_queued`` now stamps conditionally on
# ``build_state.claim_precondition``, so the database picks one winner.
# ``TestTheClaimPreconditionMatchesTheGuard`` is new and guards the thing the fix newly
# risks: the same rule now lives in two languages, and if the Mongo filter and
# ``should_enqueue`` ever disagree about a stale row, a site stops being publishable.
#
# See docs/runbooks/2026-08-11-site-build-burst-harness.md.
#
# THE HARNESS MEASURES LOGIC, NOT INFRASTRUCTURE. It cannot tell us real sandbox
# contention, real queue latency, or the right value for the concurrency cap. Those need
# a live burst, which spends real money and is the captain's call to make deliberately.
# A green run here does NOT mean the cap is validated.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import build_state as bs
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.burst_harness import (
    BUILD_TIMEOUT,
    Clock,
    FakeSite,
    RecordingPool,
    YieldingPool,
    aged_site,
    burst_on_distinct_sites,
    burst_on_one_doc,
    burst_on_reloaded_docs,
)

BURST = 8
"""Enough concurrency to interleave every ordering that matters, small enough that a
failure prints a readable report. The lane's failure mode does not need N=100 to show
up — it shows up at 2."""


# ---------------------------------------------------------------------------
# The guard works. Establish that before measuring where it stops working.
# ---------------------------------------------------------------------------


class TestTheGuardHoldsWhenPublishesAreSerialised:
    """The baseline, and the harness's own calibration. If these failed, a later red
    result would be the harness lying rather than the lane racing."""

    async def test_a_second_sequential_publish_is_refused(self) -> None:
        site = FakeSite()
        pool = RecordingPool()
        first = await bj.enqueue_site_build(
            site,
            engine="react",
            generator_input={"siteId": "x"},
            timeout_seconds=BUILD_TIMEOUT,
            _pool_override=pool,
        )
        second = await bj.enqueue_site_build(
            site,
            engine="react",
            generator_input={"siteId": "x"},
            timeout_seconds=BUILD_TIMEOUT,
            _pool_override=pool,
        )
        assert isinstance(first, str)
        assert second is None, "the guard's answer for 'already in flight' is None"
        assert pool.sandboxes == 1, "one publish, one sandbox, one bill"

    async def test_a_burst_arriving_after_the_stamp_is_refused_in_full(self) -> None:
        """The load-bearing proof that the STATE MACHINE is right. Once the row carries
        its ``queued`` stamp, N concurrent publishes all read it and all decline — no
        sandbox, no bill. Whatever goes wrong under contention is therefore the missing
        atomicity at the row, not ``should_enqueue``'s logic."""
        site = aged_site("queued", age_seconds=5)
        report = await burst_on_one_doc(site, BURST, pool=YieldingPool())
        assert report.sandboxes == 0
        assert report.refused == BURST
        assert report.errors == []


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


class TestSingleFlightUnderContention:
    """N concurrent publishes of ONE site must produce exactly one in-flight build. Two
    sandboxes for one site is two bills and two artifacts racing to deploy.

    THESE ARE THE ACCEPTANCE TESTS FOR THE ATOMIC CLAIM, and they were written first, in
    #1910, asserting the numbers the lane actually produced: ``sandboxes == BURST``. The
    gate was a read (``should_enqueue``) followed by a write (``mark_build_queued``) with
    an await between them, so every publish inside that window read the same pre-stamp row
    and each one was correct, by the guard's own rules, to open a sandbox. Eight publishes,
    eight sandboxes.

    ``claim_build_queued`` moves the decision INTO the write —
    ``build_state.claim_precondition`` as a filter on ``find_one_and_update`` — so the
    database picks one winner and the losers write nothing and return ``None``. The numbers
    below are the flipped ones.

    THE FAKE'S YIELD BEHAVIOUR IS WHAT MAKES THIS MEANINGFUL. Mongomock resolves a write
    without yielding to the event loop, so a burst driven through real Beanie against
    mongomock reported one sandbox while the lane was opening eight — the old
    infrastructure could not see this bug. ``FakeSite.set`` yields before applying and
    ``FakeCollection.find_one_and_update`` yields before matching and then does not yield
    again, which is the one guarantee a single-document Mongo update gives.
    """

    async def test_a_concurrent_burst_opens_exactly_one_sandbox(self) -> None:
        site = FakeSite()
        report = await burst_on_one_doc(site, BURST, pool=YieldingPool())
        assert report.errors == []
        assert report.sandboxes == 1, (
            f"{BURST} concurrent publishes of one site opened {report.sandboxes} sandboxes; "
            "single-flight means 1"
        )
        assert report.refused == BURST - 1
        assert len(report.job_ids) == 1

    async def test_separate_requests_each_holding_their_own_row_copy_still_get_one(self) -> None:
        """The production shape, and the one an in-process lock would not fix. Each publish
        loads the site before it claims, so no local copy can see a sibling's stamp — the
        decision has to happen at the row, which is where the precondition puts it."""
        site = FakeSite()
        report = await burst_on_reloaded_docs(site, BURST, pool=YieldingPool())
        assert report.errors == []
        assert report.sandboxes == 1
        assert report.refused == BURST - 1

    async def test_two_concurrent_publishes_get_one_sandbox(self) -> None:
        """Two clicks inside one round trip was enough to reproduce the leak, so it is
        also the smallest case that has to hold."""
        report = await burst_on_one_doc(FakeSite(), 2, pool=YieldingPool())
        assert report.sandboxes == 1
        assert report.refused == 1

    async def test_the_losing_publish_returns_none_rather_than_raising(self) -> None:
        """The contract upstream already relies on: ``None`` means "a build is already in
        flight", not "something went wrong". A loser that raised, or that rolled the row to
        ``failed``, would turn a race the user cannot see into an error they can."""
        site = FakeSite()
        report = await burst_on_one_doc(site, BURST, pool=YieldingPool())
        assert report.errors == []
        assert report.refused == BURST - 1
        assert site.build_status == "queued"
        assert site.build_reason is None

    async def test_the_losers_write_nothing_at_all(self) -> None:
        """One claim, one status write. If a loser wrote too, the row would carry a second
        ``queued`` stamp — which moves the staleness window forward on a build it does not
        own and hides a genuinely stuck build for another full window."""
        site = FakeSite()
        await burst_on_one_doc(site, BURST, pool=YieldingPool())
        assert site.transitions == [("queued", None)]

    async def test_each_rebuild_mints_a_fresh_job_id(self) -> None:
        """Still load-bearing after the fix, for a reason unrelated to the race: arq
        refuses a duplicate id by returning ``None`` and keeps a finished job's RESULT for
        an hour, so a stable per-site id would silently refuse every rebuild for an hour
        after a build finished — a single-flight guard enforced in the wrong layer, and
        invisible because the refusal is a ``None`` rather than an error."""
        pool = YieldingPool()
        site = FakeSite()
        first = await burst_on_one_doc(site, 1, pool=pool)
        site.build_status = "built"  # the first build finished; a rebuild is legitimate
        second = await burst_on_one_doc(site, 1, pool=pool)
        assert first.job_ids != second.job_ids
        assert len(set(pool.job_ids)) == 2


class TestTheGuardIsPerSiteNotGlobal:
    async def test_distinct_sites_are_not_throttled_by_each_other(self) -> None:
        """The control case. A guard that collapsed distinct sites would be a global lock
        wearing a single-flight costume, serialising every customer behind whoever
        published first — so this must stay N-for-N even after the race above is fixed."""
        report = await burst_on_distinct_sites(BURST)
        assert report.sandboxes == BURST
        assert report.refused == 0
        assert len(set(report.job_ids)) == BURST


# ---------------------------------------------------------------------------
# The staleness window, at both edges, on an injected clock
# ---------------------------------------------------------------------------


class TestTheStalenessWindowUnderBurst:
    """The window is derived from the build's OWN timeout plus ``STALE_MARGIN``, not a
    constant, and a burst is where a wrong window costs the most: every publish in it
    makes the same wrong call at once.

    Both edges matter and they fail in opposite directions — too generous pins a dead row
    (the site becomes unpublishable), too tight re-enqueues onto a healthy long build
    (two sandboxes). The clock is injected, so these land on the exact boundary second
    instead of sleeping toward it."""

    def test_a_healthy_long_build_is_never_re_enqueued_by_a_burst(self) -> None:
        clock = Clock()
        site = FakeSite(build_status="building", build_started_at=clock.ago(seconds=590))
        for _ in range(BURST):
            assert bs.should_enqueue(site, BUILD_TIMEOUT, now=clock.now) is False

    def test_a_dead_row_past_its_window_is_re_enqueued(self) -> None:
        """The one-way-door fix. Without it a build that died without writing a terminal
        status pins the row and every later publish is a silent no-op."""
        clock = Clock()
        window = bs.stale_after(BUILD_TIMEOUT).total_seconds()
        site = FakeSite(build_status="building", build_started_at=clock.ago(seconds=window + 1))
        assert bs.should_enqueue(site, BUILD_TIMEOUT, now=clock.now) is True

    def test_the_boundary_is_strictly_past_the_window_not_at_it(self) -> None:
        """Exactly at the window is still in flight. A harness that only tested "well
        past" and "well inside" would miss an off-by-one that unsticks live builds one
        second early — cheap to write, and the exact reason the clock is injected."""
        clock = Clock()
        window = bs.stale_after(BUILD_TIMEOUT)
        at_the_edge = FakeSite(build_status="building", build_started_at=clock.now - window)
        one_past = FakeSite(
            build_status="building",
            build_started_at=clock.now - window - timedelta(seconds=1),
        )
        assert bs.build_is_stale(at_the_edge, BUILD_TIMEOUT, now=clock.now) is False
        assert bs.build_is_stale(one_past, BUILD_TIMEOUT, now=clock.now) is True

    @pytest.mark.parametrize("timeout", [60, 600, 3600])
    def test_a_longer_budget_buys_a_longer_window(self, timeout: int) -> None:
        """The whole point of deriving instead of hard-coding: one build age reads as
        stale under its own budget and healthy under a larger one.

        Worth knowing while sizing the cap: ``STALE_MARGIN`` is 10 minutes, so for any
        engine budget below that the margin DOMINATES the window and deriving buys
        little. Both engines currently resolve to 600s, where the two terms are
        comparable; a much shorter budget would make the window effectively constant
        again, which is the thing deriving it was meant to avoid."""
        clock = Clock()
        age = bs.stale_after(timeout).total_seconds() + 60
        site = FakeSite(build_status="building", build_started_at=clock.ago(seconds=age))
        assert bs.should_enqueue(site, timeout, now=clock.now) is True
        assert bs.should_enqueue(site, timeout + 3600, now=clock.now) is False

    async def test_a_burst_on_a_stale_row_is_no_longer_blocked_by_it(self) -> None:
        """End to end through the enqueue: a wedged row must not survive as a permanent
        block. The overspend from the race above applies here too — but a stuck site that
        can never publish again is the failure this direction is biased against."""
        window = bs.stale_after(BUILD_TIMEOUT).total_seconds()
        site = aged_site("building", age_seconds=window + 120)
        report = await burst_on_one_doc(site, BURST, pool=YieldingPool())
        assert report.sandboxes == 1, (
            "a dead row must not block publishes forever, and unsticking it must not "
            "hand out a sandbox per publish either"
        )
        assert report.refused == BURST - 1


# ---------------------------------------------------------------------------
# queued vs building
# ---------------------------------------------------------------------------


class TestQueuedAndBuildingStayDistinguishable:
    """Collapsing these loses the only signal separating "waiting behind the cap" from
    "stuck", which is what turns a crash into a support ticket. Under load is exactly
    when that distinction is load-bearing, because under load is when queueing is
    normal."""

    async def test_a_burst_stamps_queued_and_a_worker_pickup_stamps_building(self) -> None:
        from pocketpaw_ee.sites import service as sites_service

        site = FakeSite()
        await burst_on_one_doc(site, 3, pool=YieldingPool())
        assert {status for status, _ in site.transitions} == {"queued"}
        await sites_service.mark_build_running(site)
        assert [status for status, _ in site.transitions][-1] == "building"
        assert [s for s, _ in site.transitions].count("queued") == 1, (
            "three concurrent publishes, one claim, one stamp"
        )

    async def test_a_worker_pickup_moves_the_clock_forward_not_just_the_status(self) -> None:
        """``mark_build_running`` RE-stamps ``build_started_at`` on purpose: the stamp
        means "when the current attempt started", and carrying the enqueue's clock over
        would spend the staleness window on queue wait. Under a cap that is most of the
        window, so a build that waited would be declared stale while still running and
        re-enqueued on top of itself — the expensive direction.

        Driven through the seam rather than asserted on a hand-built row, so dropping the
        re-stamp from the write actually fails something."""
        from pocketpaw_ee.sites import service as sites_service

        queued_an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
        site = FakeSite(build_status="queued", build_started_at=queued_an_hour_ago)
        await sites_service.mark_build_running(site)
        assert site.build_status == "building"
        assert site.build_started_at is not None
        assert site.build_started_at > queued_an_hour_ago + timedelta(minutes=59), (
            "the queue wait was charged to the build's window instead of being re-stamped"
        )

    def test_both_are_in_flight_but_they_are_not_the_same_state(self) -> None:
        queued = aged_site("queued", age_seconds=1)
        building = aged_site("building", age_seconds=1)
        assert bs.is_in_flight(queued) and bs.is_in_flight(building)
        assert queued.build_status != building.build_status
        assert bs.IN_FLIGHT_STATUSES == {"queued", "building"}

    def test_a_worker_pickup_re_stamps_the_clock_so_queue_wait_is_not_charged(self) -> None:
        """The reason ``mark_build_running`` re-stamps rather than leaving the enqueue's
        clock: under a cap, queue wait can be most of the window, and a build that waited
        would be declared stale while still running — then re-enqueued on top of itself,
        which is the expensive direction."""
        clock = Clock()
        waited = bs.stale_after(BUILD_TIMEOUT).total_seconds() - 30
        queued_long_ago = FakeSite(
            build_status="queued", build_started_at=clock.ago(seconds=waited)
        )
        assert bs.should_enqueue(queued_long_ago, BUILD_TIMEOUT, now=clock.now) is False
        # 60s later the enqueue stamp has lapsed; a re-stamp at pickup is what saves it.
        later = clock.advance(seconds=60)
        assert bs.should_enqueue(queued_long_ago, BUILD_TIMEOUT, now=later) is True
        restamped = FakeSite(build_status="building", build_started_at=later)
        assert bs.should_enqueue(restamped, BUILD_TIMEOUT, now=later) is False

    def test_a_queued_build_is_visible_to_a_viewer_not_silently_absent(self) -> None:
        """What makes a cap safe to turn on: a publish waiting behind it has to be
        visibly waiting. ``is_in_flight`` is the UI's read and is deliberately not
        ``not should_enqueue`` — a stale row still renders as in-progress."""
        stale = aged_site("queued", age_seconds=99_999)
        assert bs.is_in_flight(stale) is True
        assert bs.should_enqueue(stale, BUILD_TIMEOUT) is True


# ---------------------------------------------------------------------------
# The asymmetric-failure call
# ---------------------------------------------------------------------------


class TestAnUnusableStampReadsAsStale:
    """Deliberately asymmetric, and biased toward spending: a redundant enqueue costs one
    idempotent build, a stuck guard costs the site EVERY future publish. Under a burst
    the redundancy is multiplied, which is the price of the bias and is still the right
    side to be wrong on."""

    @pytest.mark.parametrize("stamp", [None, "2026-08-11", 0, object(), float("nan")])
    def test_an_unreadable_stamp_does_not_block_a_publish(self, stamp: object) -> None:
        site = FakeSite(build_status="building", build_started_at=stamp)  # type: ignore[arg-type]
        assert bs.build_is_stale(site, BUILD_TIMEOUT) is True
        assert bs.should_enqueue(site, BUILD_TIMEOUT) is True

    async def test_a_stampless_in_flight_row_never_wedges_the_site(self) -> None:
        """The precondition has to permit this row, or the asymmetric call is reversed and
        a row that never got a stamp becomes permanently unpublishable. It also must not
        permit it more than once."""
        site = FakeSite(build_status="queued", build_started_at=None)
        report = await burst_on_one_doc(site, BURST, pool=YieldingPool())
        assert report.sandboxes == 1

    def test_an_unknown_status_reads_as_terminal_and_lets_a_publish_through(self) -> None:
        """The reader/writer deploy-ordering constraint in the module header, from the
        guard's side: an unrecognised status is far likelier to be drift or a bad write
        than a live build, and blocking on it recreates the one-way door. The cost is
        that a NEW in-flight state must reach every reader before any writer emits it."""
        assert bs.should_enqueue(aged_site("provisioning", age_seconds=1), BUILD_TIMEOUT) is True
        assert bs.is_in_flight(aged_site("provisioning", age_seconds=1)) is False


# ---------------------------------------------------------------------------
# settle: None means stay in flight, and nothing may be written
# ---------------------------------------------------------------------------


class TestARetryStaysInFlightAndWritesNothing:
    """``settle`` returning ``None`` is a NO-WRITE instruction, not a status. Writing
    ``failed`` between attempts is read by ``should_enqueue`` as "free to re-publish",
    which invites a second sandbox on top of the retry — the same overspend the guard
    exists to prevent, arriving through the settle path instead of the enqueue path.

    These drive ``build_job._record`` rather than ``settle`` alone, because the bug this
    guards is at the boundary: ``settle`` can return the right thing and the row still
    get written if ``_record`` does not honour it."""

    async def test_a_retryable_failure_with_attempts_left_writes_no_status(self) -> None:
        site = aged_site("building", age_seconds=10)
        settlement = bj.BuildSettlement(status=None, reason="sandbox_unavailable:create_failed")
        await bj._record(site, settlement)
        assert site.transitions == [], "a retry must not write a status, even briefly"
        assert site.build_status == "building"
        assert bs.should_enqueue(site, BUILD_TIMEOUT) is False, (
            "the row must still block a second publish while the retry is pending"
        )

    async def test_a_terminal_settlement_is_written_once_with_its_rung(self) -> None:
        site = aged_site("building", age_seconds=10)
        await bj._record(site, bj.BuildSettlement(status="failed", reason="scaffold_failed:exit_1"))
        assert site.transitions == [("failed", "scaffold_failed:exit_1")]
        assert bs.should_enqueue(site, BUILD_TIMEOUT) is True, "a terminal row never blocks"

    async def test_concurrent_settlements_of_one_build_write_at_most_one_status(self) -> None:
        """A retry racing its own timeout is a real shape: two settlements for one build.
        The ``None`` one must contribute nothing, so the row carries exactly the terminal
        verdict and not a queue of them."""
        site = aged_site("building", age_seconds=10)
        await asyncio.gather(
            bj._record(site, bj.BuildSettlement(status=None, reason="infra_lost:retrying")),
            bj._record(site, bj.BuildSettlement(status="built", reason="completed_ok:verified")),
        )
        assert site.transitions == [("built", "completed_ok:verified")]

    def test_settle_never_leaves_a_row_in_an_in_flight_status(self) -> None:
        """The invariant that ties settle back to the guard: a finished build whose status
        is still ``queued`` or ``building`` blocks every publish until the window lapses."""
        for outcome in ("completed_ok", "build_failed", "timed_out", "infra_lost", "??"):
            for retryable in (True, False):
                got = bs.settle(outcome, retryable=retryable, attempts_left=0)
                assert got in bs.TERMINAL_STATUSES, (outcome, retryable, got)

    def test_a_retry_budget_holds_the_row_open_rather_than_terminating_it(self) -> None:
        assert bs.settle("infra_lost", retryable=True, attempts_left=1) is None
        assert bs.settle("infra_lost", retryable=True, attempts_left=0) == "failed"


# ---------------------------------------------------------------------------
# The harness itself
# ---------------------------------------------------------------------------


class TestTheClaimPreconditionMatchesTheGuard:
    """The precondition has to permit EXACTLY what ``should_enqueue`` permits.

    Two halves of one rule live in two languages now — Python for the read, a Mongo filter
    for the write — and the failure mode of drift is asymmetric and nasty. A precondition
    that is too STRICT (the obvious "status not in queued/building") refuses a stale
    in-flight row, which is the one case the window exists to let through, and the site
    becomes permanently unpublishable with no error to see. Too LOOSE and the leak is back.

    Driven against mongomock ON PURPOSE, and this is the one place that is the right tool:
    these assert what the filter MEANS to a Mongo query engine, where mongomock's lack of
    yielding is irrelevant. Atomicity is verified separately, on the yielding fake — a
    mongomock burst cannot see a race at all.
    """

    async def _claimable(self, beanie_db: object, **fields: object) -> bool:
        from pocketpaw_ee.cloud.models.site import Site

        doc = Site(workspace="ws1", pocket_id="pk1", owner="u1", name="S", **fields)  # type: ignore[arg-type]
        await doc.insert()
        found = await Site.get_pymongo_collection().find_one(
            {"_id": doc.id, **bs.claim_precondition(BUILD_TIMEOUT)}
        )
        return found is not None

    @pytest.mark.parametrize("status", ["none", "built", "failed"])
    async def test_a_terminal_row_is_claimable(self, beanie_test_db, status: str) -> None:
        assert await self._claimable(
            beanie_test_db, build_status=status, build_started_at=datetime.now(UTC)
        )

    async def test_an_unknown_status_is_claimable(self, beanie_test_db) -> None:
        """Matches ``should_enqueue``'s reading of an unrecognised status as terminal. The
        cost of this choice is the deploy-ordering constraint in ``build_state``'s header:
        a new in-flight state must reach every reader before any writer emits it."""
        assert await self._claimable(
            beanie_test_db, build_status="provisioning", build_started_at=datetime.now(UTC)
        )

    @pytest.mark.parametrize("status", ["queued", "building"])
    async def test_a_fresh_in_flight_row_is_not_claimable(
        self, beanie_test_db, status: str
    ) -> None:
        """The leak, closed. This is the row a second publish used to stamp over."""
        assert not await self._claimable(
            beanie_test_db, build_status=status, build_started_at=datetime.now(UTC)
        )

    @pytest.mark.parametrize("status", ["queued", "building"])
    async def test_a_stale_in_flight_row_is_claimable(self, beanie_test_db, status: str) -> None:
        """The case a naive status-only precondition would have refused, turning an
        overspend bug into an unpublishable site — the worse direction."""
        age = timedelta(seconds=bs.stale_after(BUILD_TIMEOUT).total_seconds() + 60)
        assert await self._claimable(
            beanie_test_db, build_status=status, build_started_at=datetime.now(UTC) - age
        )

    async def test_an_in_flight_row_at_its_exact_window_is_not_claimable(
        self, beanie_test_db
    ) -> None:
        """Same strict boundary as ``build_is_stale``: at the window it is still in
        flight. If the two disagreed by a tick, the read and the write would disagree
        about whether a live build exists."""
        at_edge = datetime.now(UTC) - bs.stale_after(BUILD_TIMEOUT)
        assert not await self._claimable(
            beanie_test_db, build_status="building", build_started_at=at_edge + timedelta(seconds=5)
        )

    async def test_an_in_flight_row_with_no_stamp_is_claimable(self, beanie_test_db) -> None:
        """The asymmetric call, preserved through the filter: one redundant idempotent
        build beats a site that can never publish again."""
        assert await self._claimable(beanie_test_db, build_status="building", build_started_at=None)

    async def test_the_claim_tests_the_window_against_the_clock_it_stamps_with(
        self, beanie_test_db
    ) -> None:
        """One clock decides the window and writes the stamp. If the precondition used
        wall-clock while the stamp used the caller's ``now``, the row would be judged
        against a window it was not written with — and a row that is live on the caller's
        clock could be claimed anyway."""
        from pocketpaw_ee.cloud.models.site import Site

        # Anchored to real now rather than a fixed date. A hard-coded date can land in the
        # future relative to wall-clock UTC, and then the broken form of this code
        # (wall-clock for the window, the caller's clock for the stamp) accidentally agrees
        # with the correct form and the test proves nothing.
        started = datetime.now(UTC) - timedelta(hours=2)
        doc = Site(
            workspace="ws1",
            pocket_id="pk1",
            owner="u1",
            name="S",
            build_status="building",
            build_started_at=started,
        )
        await doc.insert()
        # Five minutes after the build started: inside its window, so still in flight —
        # even though wall-clock is far past it.
        won = await sites_service.claim_build_queued(
            doc,
            job_id="job-1",
            timeout_seconds=BUILD_TIMEOUT,
            now=started + timedelta(minutes=5),
        )
        assert won is False
        assert (await Site.get(doc.id)).build_status == "building"

    async def test_the_precondition_agrees_with_should_enqueue_on_every_case(
        self, beanie_test_db
    ) -> None:
        """The property that matters more than any single case: for the same row, the read
        and the write must reach the same verdict. Anything else and the two halves of the
        rule have drifted."""
        window = bs.stale_after(BUILD_TIMEOUT).total_seconds()
        cases = [
            ("none", 1),
            ("built", 1),
            ("failed", 1),
            ("queued", 5),
            ("building", 5),
            ("queued", window + 60),
            ("building", window + 60),
            ("building", None),
            ("provisioning", 5),
        ]
        for status, age in cases:
            claimable = await self._claimable(
                beanie_test_db,
                build_status=status,
                build_started_at=None
                if age is None
                else datetime.now(UTC) - timedelta(seconds=age),
            )
            expected = bs.should_enqueue(aged_site(status, age_seconds=age), BUILD_TIMEOUT)
            assert claimable is expected, (status, age, claimable, expected)


class TestTheHarnessSpendsNothing:
    """A burst harness that could reach real infrastructure is a harness nobody dares
    run, and spending sandboxes is the captain's call, not a side effect of the suite."""

    async def test_no_burst_path_touches_redis_or_daytona(self) -> None:
        """Every driver takes a pool override, so ``_get_pool`` — the only code that
        reads ``POCKETPAW_REDIS_URL`` and opens a connection — is never reached."""
        pool = RecordingPool()
        await burst_on_one_doc(FakeSite(), 3, pool=pool)
        assert pool.sandboxes == 1
        assert all(call["function"] == bj.ARQ_FUNCTION_NAME for call in pool.calls)

    def test_the_recorded_enqueue_count_is_the_cost_meter(self) -> None:
        """The number these tests assert on is a sandbox count, which is a bill. Keeping
        that mapping explicit is what makes a red result legible as money."""
        pool = RecordingPool()
        assert pool.sandboxes == 0
