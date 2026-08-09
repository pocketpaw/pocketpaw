# tests/ee/sites/test_fault_ladder_build.py — the BUILD half of the fault ladder:
# rungs F1 (Daytona unconfigured), F2 (Daytona timeout / 5xx / create failure),
# F3 (a build row that cannot be consumed), F5 (verification fails) and F7 (every
# degraded path names its rung).
#
# Created 2026-08-10 (SG-7).
#
# WHY THIS FILE EXISTS AT ALL. The lane's whole design rests on one asymmetry: only a
# build that PROVED it completed may be reported to the user as broken, and the absence
# of proof is infrastructure loss. That claim is unfalsifiable by reading the code —
# every branch looks right in isolation. It becomes falsifiable only by injecting each
# fault for real and watching where the blame lands. An untested rung is a rung that
# does not exist.
#
# WHAT IS DELIBERATELY NOT ASSERTED HERE, so nobody reads a green file as more than it
# is. On this branch the lane has NO production callers: ``run_build`` is called by
# tests only, ``build_state``'s guards are called by tests only, ``Site.build_status`` is
# written by nothing, and there is no arq job for site builds — ``service._deploy_site_doc``
# still builds through the local generator. So the parts of F2/F3 that need an
# orchestrator (retry with backoff, a terminal ``failed`` row carrying a reason, an
# enqueue that can fail) are NOT proven here, because there is nothing to inject into.
# ``TestTheWiringGapIsRealAndTemporary`` pins that absence as a tripwire: when the lane
# gets wired, those tests fail and name the rungs that must then be proven for real.
#
# NO NEW PRODUCTION LOGIC WAS ADDED FOR THIS LADDER, on purpose. The captain's constraint
# on this program is to build the test scenarios first and wire the lane afterwards, so a
# rung that "needs" a new runtime check is a rung that needs a better injection instead.
#
# F5's ``node_modules`` case is the worked example, and it took two wrong turns to land.
# Draft one added a byte-scanning gate to ``daytona_build`` and asserted against it — which
# tested the gate and left the include-list unexercised. Running the real tar over a real
# node_modules tree tested the construction instead, and turned up the gap the gate would
# have masked: ``-C dist .`` packs ``dist/node_modules/``. Draft two recorded that gap as a
# passing characterisation test, which documented a 500 MB-artifact path without closing it.
# The resolution is neither: ``--exclude=./node_modules`` on the existing command removes the
# path BY CONSTRUCTION, so there is nothing to detect at runtime and nothing left open.
#
# WIRING CONTRACTS THIS TASK OWES THE NEXT ONE, recorded here because the code cannot hold
# them yet:
#
#   1. GATE A DEPLOY ON ``BuildRunResult.ok``, NEVER ON ``classification.deployable``.
#      ``deployable`` is derived from the sentinel alone and stays True when the download
#      returns zero bytes — see its docstring. Nothing verifies delivered bytes today.
#   2. WHEN DOWNLOAD VERIFICATION IS ADDED, SPLIT ITS FAILURES ON ``retryable``. A payload
#      SHORTER than the sentinel's ``artifact_bytes`` is a property of the TRANSFER and is
#      worth another attempt; one that arrives at full size and still will not open as a gzip
#      tar is a property of what the BUILD produced, and retrying only spends a second
#      sandbox reproducing it. The sentinel already carries ``artifact_bytes``, which is what
#      makes the two distinguishable. Collapsing them into one non-retryable failure turns a
#      network blip into a permanently burned publish. Keep "nothing arrived" and "half
#      arrived" as SEPARATE reasons — they send an operator to different places.
#
#      This is not just a requirement list: it was BUILT and mutation-tested, then held back
#      because 170 lines of new production logic in a lane with zero callers is a feature
#      build, and the gate's correct shape depends on how the wiring ends up consuming it.
#      The implementation lives on ``spike/sites-artifact-verification`` — a four-way
#      classification (``artifact_empty`` / ``artifact_truncated`` / ``artifact_unreadable`` /
#      ``artifact_contains_node_modules``), an ``ArtifactRejection`` carrying its own
#      ``retryable``, and ``promised_artifact_bytes`` reading the size off the sentinel rather
#      than widening ``BuildClassification``. Start there rather than from scratch.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pocketpaw_ee.sites import build_state as bs
from pocketpaw_ee.sites import daytona_build as db
from pocketpaw_ee.sites import daytona_runner as dr
from tests.ee.sites.faults import (
    EXIT_SIGKILL,
    EXIT_SIGTERM,
    EXIT_TIMEOUT,
    NODE_MODULES_PROJECT,
    DaytonaUnavailable,
    FaultyDaytonaClient,
    clean_artifact,
    daytona_unconfigured,
    ok_sentinel,
    pack_with_real_tar,
    sandbox_create_fails,
    sandbox_dies_mid_build,
    tar_is_available,
    write_project_tree,
)

REACT_FILES = {"src/App.tsx": "export default function App() { return <p>hi</p>; }"}

#: Long enough that no real elapsed time in a unit test can reach it, so a missing
#: sentinel classifies as ``infra_lost`` rather than ``timed_out``. The distinction is
#: the point of several tests below, and a small budget would silently flip them.
WELL_INSIDE_BUDGET = 100_000


# ---------------------------------------------------------------------------
# F1 — Daytona unconfigured
# ---------------------------------------------------------------------------


class TestF1DaytonaUnconfigured:
    """No credentials, no configured sandbox target.

    The captain overrode the recommended local-builder fallback in favour of
    Daytona-only, which makes "fails loudly" a REQUIREMENT rather than an accident: a
    lane that quietly built somewhere else would make a misconfigured deploy invisible
    until the day the other builder also broke.
    """

    async def test_unconfigured_daytona_refuses_the_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daytona_unconfigured(monkeypatch)
        with pytest.raises(RuntimeError) as err:
            await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600)
        assert "not configured" in str(err.value)

    async def test_the_error_names_what_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator reading this in a log must learn which env vars to set. "Daytona
        is not available" would be a true statement and a useless one."""
        daytona_unconfigured(monkeypatch)
        with pytest.raises(RuntimeError) as err:
            await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600)
        message = str(err.value)
        assert "DAYTONA_API_URL" in message
        assert "DAYTONA_API_KEY" in message

    async def test_it_raises_rather_than_returning_a_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure mode this rung guards is not an exception — it is a RETURN. A
        result object carrying ``deployable=False`` would let a caller treat "we never
        tried" as "the build did not work", and the user would be told their site is
        broken because an env var is unset."""
        daytona_unconfigured(monkeypatch)
        with pytest.raises(RuntimeError):
            result = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600)
            pytest.fail(f"expected a raise, got a result: {result!r}")

    def test_the_lane_has_no_local_builder_fallback(self) -> None:
        """A structural guard, because this is the rung most likely to be "helpfully"
        undone later. The obvious fix for a failing Daytona is to fall back to the local
        generator, and it is exactly what the captain ruled out — a silent fallback makes
        capacity loss undetectable. If a future change imports the local builder into
        this lane, this fails and sends the author back to the ruling.

        Note the residue this does NOT catch, reported rather than deleted per the task
        brief: ``daytona_build.BuildClassification.retryable`` still documents itself as
        "May the lane retry (and then fall back to the local builder)?", and
        ``test_daytona_runner.py`` still refers to the fallback firing. Both are stale
        prose from before the override; neither is executable.
        """
        source = Path(dr.__file__).read_text(encoding="utf-8")
        for forbidden in ("GeneratorClient", "generator_client", "local_server"):
            assert forbidden not in source, (
                f"{forbidden} appeared in the Daytona lane — the captain's ruling is "
                "Daytona-only, and a local fallback must not be reintroduced"
            )


# ---------------------------------------------------------------------------
# F2 — Daytona timeout / 5xx / sandbox-create failure
# ---------------------------------------------------------------------------


class TestF2DaytonaInfrastructureFailure:
    """Every way the sandbox itself can fail must classify as infrastructure.

    The retry-with-backoff and terminal-``failed``-carrying-a-reason halves of this rung
    are NOT proven here — no orchestrator exists to retry or to write a row. See
    ``TestTheWiringGapIsRealAndTemporary``.
    """

    async def test_create_failure_propagates_and_bills_nothing(self) -> None:
        """A create that 5xxes means nothing ran, so there is no build result to report —
        the caller must see an exception and treat it as retryable. And with no sandbox,
        there is nothing to tear down."""
        client = sandbox_create_fails()
        with pytest.raises(DaytonaUnavailable):
            await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert client.calls == ["create"]
        assert "delete" not in client.calls

    async def test_a_failure_after_create_still_tears_the_sandbox_down(self) -> None:
        """The expensive mistake in this rung: a sandbox that outlives a failed build is
        a bill nobody sees. ``wait_for_sandbox`` failing is the narrowest window — the
        sandbox exists but nothing has run — and the teardown must still fire."""
        client = FaultyDaytonaClient(fail_at="wait")
        with pytest.raises(DaytonaUnavailable):
            await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert "delete" in client.calls

    async def test_an_upload_failure_still_tears_the_sandbox_down(self) -> None:
        client = FaultyDaytonaClient(fail_at="upload")
        with pytest.raises(DaytonaUnavailable):
            await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert "delete" in client.calls

    async def test_a_sandbox_that_dies_mid_build_is_infra_not_the_users_fault(self) -> None:
        """The central case. An exec that raises looks IDENTICAL to a broken build; only
        the absence of a sentinel separates them, and it must land on infrastructure."""
        client = sandbox_dies_mid_build()
        got = await dr.run_build(
            REACT_FILES, engine="react", timeout_seconds=WELL_INSIDE_BUDGET, client=client
        )
        assert got.classification.outcome == "infra_lost"
        assert got.classification.blames_user is False
        assert got.classification.retryable is True

    async def test_an_unreadable_sentinel_read_is_infra_not_the_users_fault(self) -> None:
        """A transport failure ON THE READ, rather than a missing file. Same conclusion:
        evidence we cannot obtain is evidence that does not exist."""
        client = FaultyDaytonaClient(fail_at="sentinel")
        got = await dr.run_build(
            REACT_FILES, engine="react", timeout_seconds=WELL_INSIDE_BUDGET, client=client
        )
        assert got.classification.outcome == "infra_lost"
        assert got.classification.blames_user is False

    @pytest.mark.parametrize("signal_exit", [EXIT_SIGKILL, EXIT_SIGTERM])
    @pytest.mark.parametrize("step", ["install_exit", "build_exit"])
    async def test_a_signalled_death_is_infra_even_though_it_has_a_sentinel(
        self, step: str, signal_exit: int
    ) -> None:
        """The residual gap the design calls out: a signalled process STILL RUNS THE
        TRAP, so an OOM kill arrives with a sentinel and a non-zero exit — the exact
        shape of a genuine failure. Getting this wrong reports a capacity problem as the
        user's bug, on both steps and both signals."""
        client = FaultyDaytonaClient(sentinel=ok_sentinel(**{step: signal_exit}))
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.classification.outcome == "infra_lost"
        assert got.classification.blames_user is False
        assert str(signal_exit) in got.classification.reason

    @pytest.mark.parametrize("step", ["install_exit", "build_exit"])
    async def test_the_in_sandbox_timeout_is_a_timeout_not_a_broken_build(self, step: str) -> None:
        """``timeout(1)`` firing is better evidence than our clock: we know WHICH step
        overran and we still have its stderr. It must not be read as exit-non-zero."""
        client = FaultyDaytonaClient(
            sentinel=ok_sentinel(**{step: EXIT_TIMEOUT}, stderr_tail="still installing")
        )
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.classification.outcome == "timed_out"
        assert got.classification.blames_user is False
        assert "still installing" in got.classification.stderr_tail

    async def test_no_sentinel_past_the_budget_is_a_timeout_not_a_loss(self) -> None:
        """With the budget already exhausted, "no proof" is explained by the timeout, and
        saying so is honest and actionable — an enormous site really did overrun."""
        client = sandbox_dies_mid_build()
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=0, client=client)
        assert got.classification.outcome == "timed_out"
        assert got.classification.retryable is True
        assert got.classification.blames_user is False

    async def test_only_a_proven_non_zero_build_ever_blames_the_user(self) -> None:
        """The one rung that must reach the user, and the proof that the others are not
        merely being suppressed: a real non-zero build DOES blame them, and carries the
        compiler error that makes it actionable."""
        client = FaultyDaytonaClient(sentinel=ok_sentinel(build_exit=1, stderr_tail="TS2304"))
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.classification.outcome == "build_failed"
        assert got.classification.blames_user is True
        assert "TS2304" in got.classification.stderr_tail


# ---------------------------------------------------------------------------
# F3 — a build row nothing will ever consume
# ---------------------------------------------------------------------------


class TestF3AnUnconsumableBuildRow:
    """The rung that only exists because publish went async.

    The specific harm: a publish enqueues, the enqueue fails (or the worker dies before
    writing a terminal status), and the row sits in ``queued`` with nothing coming to
    consume it. Status alone is a ONE-WAY DOOR, so every later publish becomes a silent
    no-op and the site is permanently unpublishable with no error to see.

    The enqueue itself cannot be fault-injected on this branch — there is no enqueue (see
    the wiring-gap tests). What IS injectable, and is the property that actually protects
    the user, is the recovery: whatever state a failed enqueue leaves behind, the site
    must still be republishable afterwards.
    """

    class _Row:
        """The two persisted fields the guard reads. A stand-in for the Site doc so these
        stay pure — constructing a Beanie document would drag a DB into a decision that
        touches neither."""

        def __init__(self, status: str, started_at: datetime | None) -> None:
            self.build_status = status
            self.build_started_at = started_at

    def test_a_fresh_in_flight_row_blocks_a_second_build(self) -> None:
        """Single-flight works — the baseline the recovery must not break. Two publishes
        of one site must not open two sandboxes and race on the artifact."""
        row = self._Row("queued", datetime.now(UTC))
        assert bs.should_enqueue(row, 600) is False

    def test_a_row_stuck_in_queued_becomes_republishable_once_the_window_lapses(self) -> None:
        """The recovery. A job no worker ever consumed leaves exactly this row, and the
        derived window is what unsticks it."""
        stamp = datetime.now(UTC) - bs.stale_after(600) - timedelta(seconds=1)
        row = self._Row("queued", stamp)
        assert bs.should_enqueue(row, 600) is True

    def test_a_queued_row_with_no_stamp_is_republishable_immediately(self) -> None:
        """The shape a HALF-COMPLETED enqueue leaves: status written, stamp not. It reads
        as stale on purpose — a redundant build costs one idempotent job, while a stuck
        guard costs the site every future publish. The asymmetry only pays off in this
        direction."""
        row = self._Row("queued", None)
        assert bs.should_enqueue(row, 600) is True

    def test_an_unreadable_stamp_is_republishable_too(self) -> None:
        """A garbage stamp (a string from an older writer, a None-like) must not read as
        "recent". Failing closed here would wedge the site."""
        row = self._Row("queued", "2026-08-10T00:00:00Z")  # type: ignore[arg-type]
        assert bs.should_enqueue(row, 600) is True

    def test_a_building_row_recovers_on_the_same_terms(self) -> None:
        """A worker that died mid-build leaves ``building``, not ``queued``. Both are
        in-flight, so both must unstick."""
        stamp = datetime.now(UTC) - bs.stale_after(600) - timedelta(seconds=1)
        row = self._Row("building", stamp)
        assert bs.should_enqueue(row, 600) is True

    def test_a_stale_row_still_renders_as_in_flight_to_a_viewer(self) -> None:
        """The deliberate divergence between the two predicates: the service may spend
        money again while the UI still shows progress. Collapsing them would either
        block the republish or blank the user's screen mid-build."""
        stamp = datetime.now(UTC) - bs.stale_after(600) - timedelta(seconds=1)
        row = self._Row("queued", stamp)
        assert bs.should_enqueue(row, 600) is True
        assert bs.is_in_flight(row) is True

    def test_a_terminal_failure_never_blocks_the_next_publish(self) -> None:
        """One bad build must not wedge the site until someone edits the database."""
        for status in ("failed", "built", "none"):
            row = self._Row(status, datetime.now(UTC))
            assert bs.should_enqueue(row, 600) is True, status

    def test_the_window_is_derived_from_the_build_budget_not_a_constant(self) -> None:
        """A constant window is wrong in both directions — too short re-enqueues on top
        of a healthy long build (two sandboxes, two bills), too long blocks publishes for
        half an hour. So a bigger budget must produce a bigger window."""
        assert bs.stale_after(1200) > bs.stale_after(600)
        assert bs.stale_after(600) > timedelta(seconds=600)

    def test_a_non_positive_budget_still_yields_a_usable_window(self) -> None:
        """A zero or negative timeout must not collapse the window to nothing — every
        in-flight build would read as stale and single-flight would be off entirely."""
        assert bs.stale_after(0) == bs.STALE_MARGIN
        assert bs.stale_after(-99) == bs.STALE_MARGIN

    def test_a_long_healthy_build_is_not_re_enqueued_under_it(self) -> None:
        """The other half of the derived window: a build 20 minutes into a 30-minute
        budget is HEALTHY, and re-enqueueing it would be the expensive false positive."""
        row = self._Row("building", datetime.now(UTC) - timedelta(minutes=20))
        assert bs.should_enqueue(row, 1800) is False


# ---------------------------------------------------------------------------
# F5 — verification fails
# ---------------------------------------------------------------------------


class TestF5VerificationFailsSoNothingDeploys:
    """Missing sentinel, empty artifact, or ``node_modules`` in the artifact.

    The requirement is stronger than "the publish fails": there must be NO deploy. A
    partial deploy and a blank site are worse than a failed publish, because they
    overwrite something that was working.

    The ``node_modules`` case is exercised against the REAL tar in
    ``TestF5TheIncludeListIsWhatExcludesNodeModules`` below, not against a scanner here.
    The property that protects the user is that the include-list cannot pick node_modules
    up in the first place, and only running the command can show that.
    """

    async def test_a_missing_sentinel_never_reaches_the_artifact_download(self) -> None:
        """"No deploy" starts before the deploy: an unproven build must not even fetch an
        artifact, because fetching one is the step that makes deploying it possible."""
        client = FaultyDaytonaClient(sentinel=None)
        got = await dr.run_build(
            REACT_FILES, engine="react", timeout_seconds=WELL_INSIDE_BUDGET, client=client
        )
        assert got.classification.deployable is False
        assert "download_artifact" not in client.calls
        assert got.artifact is None

    async def test_a_sentinel_reporting_zero_bytes_never_downloads_either(self) -> None:
        """The empty-deploy failure caught at the sentinel: every step reported success
        and produced nothing. Exit 0 is not sufficient."""
        client = FaultyDaytonaClient(sentinel=ok_sentinel(artifact_bytes=0))
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.classification.reason == "artifact_empty"
        assert got.classification.deployable is False
        assert "download_artifact" not in client.calls

    async def test_a_sentinel_with_no_size_at_all_fails_closed(self) -> None:
        """A sentinel that parsed but carries no size tells us nothing about the output,
        so it must not deploy. Absent must not read as fine."""
        client = FaultyDaytonaClient(sentinel=ok_sentinel(artifact_bytes=None))
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.classification.reason == "artifact_size_unknown"
        assert got.classification.deployable is False

    async def test_an_empty_download_is_reported_as_not_ok(self) -> None:
        """A CHARACTERISATION test, recording a discrepancy rather than a fix.

        The sentinel is the build's claim; the download is the fact. When the sentinel
        promises bytes and the download delivers none, ``BuildRunResult.ok`` correctly
        reads False — but ``classification.deployable`` still reads True, and that flag's
        docstring promises "there is a verified artifact to deploy". So a caller gating on
        ``deployable`` rather than ``ok`` would deploy nothing over something working.

        Left as-is deliberately: this is the proving phase, no caller exists yet, and
        adding production code to close it is the wiring phase's call. The contract for
        whoever wires this lane is "gate on ``.ok``", and this test fails if the
        discrepancy is closed — at which point the contract can be relaxed.
        """
        client = FaultyDaytonaClient(artifact=b"")
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.ok is False, "ok must not be True for a zero-byte artifact"
        assert got.artifact_bytes == 0
        assert got.classification.deployable is True, (
            "deployable now disagrees with ok — if it was fixed, drop the 'gate on .ok' "
            "contract from the SG-7 findings"
        )

    async def test_a_download_failure_is_transport_loss_not_a_build_failure(self) -> None:
        """The sentinel proved the artifact existed and was non-empty, so failing to fetch
        it cannot be the user's build being wrong."""
        client = FaultyDaytonaClient(fail_at="artifact")
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.classification.reason == "artifact_download_failed"
        assert got.classification.blames_user is False
        assert got.classification.deployable is False

    async def test_a_healthy_build_still_deploys(self) -> None:
        """A ladder is worthless if every rung fails. This is what proves the rejections
        above are discriminating rather than just failing."""
        client = FaultyDaytonaClient(artifact=clean_artifact())
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.classification.outcome == "completed_ok"
        assert got.classification.deployable is True
        assert got.ok is True
        assert got.artifact_bytes > 0


@pytest.mark.skipif(not tar_is_available(), reason="needs a real tar binary")
class TestF5TheIncludeListIsWhatExcludesNodeModules:
    """F5's ``node_modules`` case, injected against the REAL tar.

    ``node_modules`` goes on disk and the actual command from
    :func:`daytona_build.artifact_tar_command` runs over it. That tests the CONSTRUCTION —
    an include-list of exactly one directory — rather than a scanner's opinion about
    bytes. The risk being guarded is somebody rewriting this as an exclude-list of junk,
    which would ship node_modules the moment the list missed an entry, and this fails
    loudly if they do.

    A fake Daytona client cannot cover this: it records ``execute_command`` and never runs
    the wrapper, so no tar executes inside it and the artifact is whatever the fake was
    told to return.
    """

    def test_a_sibling_node_modules_is_not_packed(self, tmp_path) -> None:
        """The 500 MB-on-the-wire failure, and the shape ``bun install`` actually
        produces: ``node_modules`` next to ``dist``. ``-C <project>/dist .`` cannot reach
        it, so there is no filter to get wrong."""
        project = write_project_tree(tmp_path / "proj", NODE_MODULES_PROJECT)
        members = pack_with_real_tar(
            "react", project, str(tmp_path / "out.tgz").replace("\\", "/")
        )
        assert not any("node_modules" in m for m in members), members

    def test_the_static_output_is_actually_packed(self, tmp_path) -> None:
        """The other half, and the reason the assertion above is not vacuous: an empty tar
        would also contain no node_modules. The real output has to be in there."""
        project = write_project_tree(tmp_path / "proj", NODE_MODULES_PROJECT)
        members = pack_with_real_tar(
            "react", project, str(tmp_path / "out.tgz").replace("\\", "/")
        )
        assert "./index.html" in members
        assert "./assets/app.js" in members

    def test_a_node_modules_nested_inside_the_output_dir_is_excluded(self, tmp_path) -> None:
        """The gap SG-7 measured, now closed BY CONSTRUCTION rather than detected at runtime.

        ``-C <project>/dist .`` cannot reach a SIBLING node_modules, but it packs one nested
        inside the output dir — so the scope alone never made the 500 MB artifact impossible.
        ``--exclude=./node_modules`` prevents it instead of rejecting it after the fact, which
        matters because a runtime rejection keeps firing while the command still looks
        correct, and the cause never gets traced.
        """
        tree = dict(NODE_MODULES_PROJECT)
        tree["dist/node_modules/leaked/index.js"] = b"module.exports = {}"
        tree["dist/node_modules/.bin/vite"] = b"#!/usr/bin/env node"
        project = write_project_tree(tmp_path / "proj", tree)
        members = pack_with_real_tar(
            "react", project, str(tmp_path / "out.tgz").replace("\\", "/")
        )
        assert not any("node_modules" in m for m in members), members
        # Not vacuous: an empty tar would also satisfy the assertion above.
        assert "./index.html" in members

    def test_a_deeper_nested_node_modules_is_excluded_too(self, tmp_path) -> None:
        """A copied dependency tree does not have to land at the top of the output.

        Passes on bsdtar, whose exclude matching is unanchored. GNU tar — what the Daytona
        image actually runs — anchors a pattern containing a slash, so this case is NOT
        guaranteed there. Recorded as a test rather than a comment because it is the honest
        boundary of what has been proven locally, and the real-sandbox round-trip is what
        settles it.
        """
        tree = dict(NODE_MODULES_PROJECT)
        tree["dist/sub/node_modules/dep/index.js"] = b"module.exports = {}"
        project = write_project_tree(tmp_path / "proj", tree)
        members = pack_with_real_tar(
            "react", project, str(tmp_path / "out.tgz").replace("\\", "/")
        )
        assert not any("node_modules" in m for m in members), members

    def test_an_innocently_named_file_is_still_packed(self, tmp_path) -> None:
        """The exclusion must not fire on a legitimate ``node_modules_report.html`` in the
        output. A pattern that rejects innocent sites is how a safety measure gets removed
        rather than fixed."""
        tree = dict(NODE_MODULES_PROJECT)
        tree["dist/docs/node_modules_report.html"] = b"<p>size audit</p>"
        project = write_project_tree(tmp_path / "proj", tree)
        members = pack_with_real_tar(
            "react", project, str(tmp_path / "out.tgz").replace("\\", "/")
        )
        assert "./docs/node_modules_report.html" in members

    def test_an_engine_whose_output_is_the_project_root_is_refused(self) -> None:
        """html's output IS the project root, so an include-list cannot exclude anything.
        Refusing outright is what keeps the guarantee above true for every engine that
        does reach this lane."""
        with pytest.raises(ValueError, match="project root"):
            db.artifact_tar_command("html", "/home/daytona/paw-build", "/tmp/out.tgz")


# ---------------------------------------------------------------------------
# F7 — every degraded path names its rung
# ---------------------------------------------------------------------------


def _all_classifications() -> dict[str, db.BuildClassification]:
    """Every condition ``classify_build`` can be driven into, by name.

    Enumerated as a table rather than asserted case by case because the property under
    test is about the SET — that no two conditions collide on one reason, and that none
    is nameless. Neither can be checked one test at a time.
    """
    long_budget = WELL_INSIDE_BUDGET
    return {
        "ok": db.classify_build(ok_sentinel(), elapsed_seconds=1, timeout_seconds=long_budget),
        "no_sentinel_lost": db.classify_build(
            None, elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "no_sentinel_timeout": db.classify_build(None, elapsed_seconds=99, timeout_seconds=1),
        "unparseable": db.classify_build(
            b"{not json", elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "unknown_schema": db.classify_build(
            ok_sentinel(schema=99), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "install_oom": db.classify_build(
            ok_sentinel(install_exit=EXIT_SIGKILL),
            elapsed_seconds=1,
            timeout_seconds=long_budget,
        ),
        "build_oom": db.classify_build(
            ok_sentinel(build_exit=EXIT_SIGKILL), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "build_sigterm": db.classify_build(
            ok_sentinel(build_exit=EXIT_SIGTERM), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "install_timeout": db.classify_build(
            ok_sentinel(install_exit=EXIT_TIMEOUT), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "build_timeout": db.classify_build(
            ok_sentinel(build_exit=EXIT_TIMEOUT), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "missing_exit": db.classify_build(
            ok_sentinel(build_exit=None), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "install_failed": db.classify_build(
            ok_sentinel(install_exit=1), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "build_failed": db.classify_build(
            ok_sentinel(build_exit=1), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "size_unknown": db.classify_build(
            ok_sentinel(artifact_bytes="big"), elapsed_seconds=1, timeout_seconds=long_budget
        ),
        "empty": db.classify_build(
            ok_sentinel(artifact_bytes=0), elapsed_seconds=1, timeout_seconds=long_budget
        ),
    }


class TestF7EveryDegradedPathNamesItsRung:
    """No bare ``failed`` with no explanation.

    The harm is diagnostic, and it lands on whoever is on call: a lane that reports
    "failed" without naming the rung forces a live investigation to reconstruct which of
    a dozen conditions occurred, from a container that no longer exists.
    """

    def test_every_condition_carries_a_reason(self) -> None:
        for name, got in _all_classifications().items():
            assert got.reason, f"{name} produced a classification with no reason"

    def test_every_reason_is_machine_readable(self) -> None:
        """Reasons ride logs and metrics, so they must be greppable identifiers rather
        than prose — a reason with spaces or capitals becomes a dashboard nobody can
        group by."""
        for name, got in _all_classifications().items():
            assert got.reason.replace("_", "").isalnum(), f"{name}: {got.reason!r}"
            assert got.reason == got.reason.lower(), f"{name}: {got.reason!r}"

    def test_distinct_conditions_do_not_share_a_reason(self) -> None:
        """Two conditions with one reason is the same diagnostic failure as no reason at
        all — an operator reading it still cannot tell which happened.

        The two legitimate collisions are asserted rather than excused: unparseable and
        unknown-schema really are the same fact (a sentinel we cannot read, which is
        indistinguishable from absence), and it is on purpose that they classify
        identically.
        """
        table = _all_classifications()
        unreadable = {"unparseable", "unknown_schema", "no_sentinel_lost"}
        assert len({table[k].reason for k in unreadable}) == 1

        distinct = {k: v.reason for k, v in table.items() if k not in unreadable}
        assert len(set(distinct.values())) == len(distinct), (
            f"reasons collided across distinct conditions: {sorted(distinct.items())}"
        )

    def test_no_infrastructure_outcome_ever_blames_the_user(self) -> None:
        """The cross-cutting rung, restated over the whole table: whatever else changes,
        ``blames_user`` may only be True for a proven ``build_failed``."""
        for name, got in _all_classifications().items():
            if got.blames_user:
                assert got.outcome == "build_failed", f"{name} blamed the user for {got.outcome}"

    def test_only_a_proven_build_failure_can_blame_the_user(self) -> None:
        """The converse: an outcome that is not ``build_failed`` must never be presentable
        as the user's fault, and ``build_failed`` is only reachable WITH a sentinel."""
        table = _all_classifications()
        blamed = {k for k, v in table.items() if v.blames_user}
        assert blamed == {"install_failed", "build_failed", "size_unknown", "empty"}

    async def test_the_runner_preserves_the_reason_it_was_given(self) -> None:
        """A reason that is correct in the classifier and dropped by the runner is not a
        reason. This is the seam where it would be lost."""
        client = FaultyDaytonaClient(sentinel=ok_sentinel(build_exit=EXIT_SIGKILL))
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=client)
        assert got.classification.reason == f"build_killed_by_signal_{EXIT_SIGKILL}"

    async def test_the_runner_only_reason_is_named_too(self) -> None:
        """One condition exists only in the runner and the classifier never sees it, which
        makes it the one most likely to ship nameless."""
        download = FaultyDaytonaClient(fail_at="artifact")
        got = await dr.run_build(REACT_FILES, engine="react", timeout_seconds=600, client=download)
        assert got.classification.reason == "artifact_download_failed"
        assert got.classification.blames_user is False


# ---------------------------------------------------------------------------
# The gap — pinned so it cannot close silently
# ---------------------------------------------------------------------------


class TestTheWiringGapIsRealAndTemporary:
    """Tripwires, not assertions that the gap is GOOD.

    These pin the fact that the lane has no production callers on this branch, which is
    why parts of F2/F3 are unproven. Each one FAILS the day someone wires the lane —
    which is exactly when the rung it names becomes injectable and must be proven for
    real. Deleting a failing test here instead of proving its rung is the one wrong
    response.
    """

    def test_publish_does_not_consult_the_daytona_lane_yet(self) -> None:
        """When this fails: F1's publish half is now provable — a publish with Daytona
        unconfigured must report unavailable and leave the site's row untouched."""
        from pocketpaw_ee.sites import service

        source = Path(service.__file__).read_text(encoding="utf-8")
        assert "daytona_runner" not in source, (
            "the Daytona lane is now wired into publish — prove F1's publish half "
            "(unavailable + site unchanged) and F2's terminal failed-with-reason"
        )

    def test_nothing_writes_the_build_lifecycle_fields_yet(self) -> None:
        """When this fails: F2's "terminal ``failed`` carrying a reason" and F7's
        "the recorded status names its rung" become provable, and must be proven. Note
        there is no ``build_reason`` field on the model today — a rung name has nowhere
        to be recorded, which is itself a finding for whoever wires this."""
        from pocketpaw_ee.sites import service

        source = Path(service.__file__).read_text(encoding="utf-8")
        assert "build_status" not in source, (
            "something now writes Site.build_status — prove that a terminal failure "
            "records a reason naming its rung, and that a failed enqueue cannot pin "
            "the row in queued"
        )

    def test_there_is_no_enqueue_for_site_builds_yet(self) -> None:
        """When this fails: F3's enqueue injection becomes real — patch the arq pool to
        raise and assert the row does not end up pinned in ``queued``."""
        from pocketpaw_ee.sites import service

        source = Path(service.__file__).read_text(encoding="utf-8")
        # Two markers because either alone is evadable: a wiring that goes through a
        # helper never names ``enqueue_job``, and one that skips the polling handle never
        # names ``build_job_id``. Any enqueue a client can poll writes at least one.
        for marker in ("enqueue_job", "build_job_id"):
            assert marker not in source, (
                f"site builds now reach an enqueue ({marker!r}) — inject an arq/Redis "
                "failure at the enqueue and prove the site is still republishable"
            )

    def test_the_lane_still_classifies_one_attempt_at_a_time(self) -> None:
        """When this fails: F2's retry-with-backoff becomes provable. ``FaultyDaytonaClient``
        already takes ``fail_times`` for it — fail the first two attempts, assert the third
        succeeded and that the backoff actually slept."""
        import inspect

        source = inspect.getsource(dr.run_build)
        for retry_marker in ("for attempt", "while attempt", "backoff", "sleep"):
            assert retry_marker not in source, (
                f"run_build appears to retry ({retry_marker!r}) — prove F2's backoff and "
                "the terminal failed-with-reason on give-up"
            )
