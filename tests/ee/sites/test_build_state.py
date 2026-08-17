# tests/ee/sites/test_build_state.py — the ephemeral-build lane's lifecycle guard
# (ee/pocketpaw_ee/sites/build_state.py).
#
# Created 2026-08-09 (SG-9i, async publish).
#
# THE TWO FAILURES THESE GUARD, both of which have shipped before in the provision path:
#
#   1. A ONE-WAY DOOR. A build that dies without writing a terminal status pins the row
#      in ``building`` and every later publish becomes a silent no-op — a site nobody
#      can republish, with no error anywhere to see. The staleness window is the only
#      thing that reopens it, so the tests below care much more about "does a dead row
#      unstick" than about "does a live row block".
#
#   2. SPENDING TWICE. The mirror failure: re-enqueueing on top of a healthy long build
#      means two sandboxes, two bills, and two artifacts racing to be the one that
#      deploys. That is why the window is derived from the build's own timeout rather
#      than a constant.
#
# Mutations in tests/mutations/build_state.json, run and caught.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pocketpaw_ee.sites import build_state as bs

TIMEOUT = 600


def _site(status: str, *, age_seconds: float | None = None, stamp: object = ...) -> SimpleNamespace:
    """A Site-shaped stand-in. ``age_seconds`` sets a stamp that far in the past;
    ``stamp`` sets one verbatim (for the unusable-value cases)."""
    if stamp is not ...:
        return SimpleNamespace(build_status=status, build_started_at=stamp)
    started = None if age_seconds is None else datetime.now(UTC) - timedelta(seconds=age_seconds)
    return SimpleNamespace(build_status=status, build_started_at=started)


class TestATerminalRowNeverBlocks:
    @pytest.mark.parametrize("status", ["none", "built", "failed"])
    def test_terminal_statuses_allow_a_new_build(self, status: str) -> None:
        assert bs.should_enqueue(_site(status, age_seconds=1), TIMEOUT) is True

    def test_failed_does_not_wedge_the_site(self) -> None:
        """If ``failed`` blocked, one bad build would wedge the site until somebody
        edited the database by hand."""
        assert bs.should_enqueue(_site("failed", age_seconds=0), TIMEOUT) is True

    def test_built_allows_a_rebuild(self) -> None:
        """A site is provisioned once and rebuilt many times — that is the whole reason
        these fields are separate from the provision_* trio."""
        assert bs.should_enqueue(_site("built", age_seconds=0), TIMEOUT) is True


class TestALiveBuildBlocks:
    def test_fresh_building_blocks(self) -> None:
        assert bs.should_enqueue(_site("building", age_seconds=5), TIMEOUT) is False

    def test_fresh_queued_blocks(self) -> None:
        """A queued build has no sandbox yet, but it is still in flight — enqueueing a
        second would spend twice for one publish."""
        assert bs.should_enqueue(_site("queued", age_seconds=5), TIMEOUT) is False

    def test_a_build_inside_its_own_timeout_still_blocks(self) -> None:
        """The mirror failure: re-enqueueing on top of a healthy long build means two
        sandboxes and two artifacts racing to deploy."""
        assert bs.should_enqueue(_site("building", age_seconds=TIMEOUT - 1), TIMEOUT) is False


class TestADeadRowUnsticks:
    def test_past_the_window_a_stuck_row_allows_a_new_build(self) -> None:
        """The one-way-door fix. Without this the site is permanently unpublishable."""
        age = TIMEOUT + bs.STALE_MARGIN.total_seconds() + 60
        assert bs.should_enqueue(_site("building", age_seconds=age), TIMEOUT) is True

    def test_a_missing_stamp_reads_as_stale(self) -> None:
        """Asymmetric failure, deliberately biased: a redundant enqueue costs one
        idempotent build, a stuck guard costs every future publish. Mutation: flip this
        to False and a row with no stamp becomes permanently unpublishable."""
        assert bs.build_is_stale(_site("building", age_seconds=None), TIMEOUT) is True
        assert bs.should_enqueue(_site("building", age_seconds=None), TIMEOUT) is True

    @pytest.mark.parametrize("bad", [None, "2026-08-09", 0, object()])
    def test_an_unreadable_stamp_reads_as_stale(self, bad: object) -> None:
        assert bs.build_is_stale(_site("building", stamp=bad), TIMEOUT) is True

    def test_a_naive_stamp_is_assumed_utc_not_rejected(self) -> None:
        """Treating a naive datetime as unreadable would make every row written by an
        older writer look stale, which quietly disables the guard."""
        naive = datetime.now(UTC).replace(tzinfo=None)
        assert bs.build_is_stale(_site("building", stamp=naive), TIMEOUT) is False


class TestTheWindowIsDerivedNotConstant:
    def test_window_scales_with_the_build_timeout(self) -> None:
        """DP0-4 used a flat 30 minutes. A constant is wrong in both directions — too
        short re-enqueues onto a healthy long build, too long leaves a stuck row
        blocking publishes."""
        assert bs.stale_after(600) < bs.stale_after(3600)

    def test_window_always_exceeds_the_timeout(self) -> None:
        """Sandbox create, upload, extract and teardown all sit OUTSIDE the in-sandbox
        timeout, so the window has to cover more than the build itself."""
        for timeout in (0, 60, 600, 3600):
            assert bs.stale_after(timeout) > timedelta(seconds=timeout)

    def test_a_nonpositive_timeout_still_yields_a_positive_window(self) -> None:
        """A zero or negative window would make every in-flight build read as stale and
        defeat the guard entirely."""
        assert bs.stale_after(0) == bs.STALE_MARGIN
        assert bs.stale_after(-999) == bs.STALE_MARGIN

    def test_a_long_build_is_not_declared_stale_by_a_short_window(self) -> None:
        age = 3000
        assert bs.should_enqueue(_site("building", age_seconds=age), 600) is True
        assert bs.should_enqueue(_site("building", age_seconds=age), 7200) is False


class TestInFlightIsForTheUiNotTheWallet:
    def test_a_stale_row_still_reads_as_in_flight_to_a_viewer(self) -> None:
        """Deliberately different from ``not should_enqueue``: a stale row must not
        BLOCK a publish, but it should still render as in-progress rather than
        silently as idle."""
        stale = _site("building", age_seconds=99_999)
        assert bs.is_in_flight(stale) is True
        assert bs.should_enqueue(stale, TIMEOUT) is True

    @pytest.mark.parametrize("status", ["none", "built", "failed"])
    def test_terminal_is_not_in_flight(self, status: str) -> None:
        assert bs.is_in_flight(_site(status, age_seconds=1)) is False

    def test_queued_is_in_flight(self) -> None:
        """The state that makes the concurrency cap safe to turn on: a publish waiting
        behind the cap has to be visibly waiting, not silently absent."""
        assert bs.is_in_flight(_site("queued", age_seconds=1)) is True


class TestStatusVocabulary:
    def test_queued_exists_as_a_distinct_state(self) -> None:
        """Without it, a publish waiting behind the cap is indistinguishable from a hung
        one and the cap turns crashes into support tickets."""
        assert "queued" in bs.IN_FLIGHT_STATUSES
        assert "queued" not in bs.TERMINAL_STATUSES

    def test_in_flight_and_terminal_partition_the_vocabulary(self) -> None:
        assert bs.IN_FLIGHT_STATUSES.isdisjoint(bs.TERMINAL_STATUSES)
        assert bs.IN_FLIGHT_STATUSES | bs.TERMINAL_STATUSES == {
            "none",
            "queued",
            "building",
            "built",
            "failed",
        }

    def test_an_unknown_status_does_not_block_a_publish(self) -> None:
        """Fail open on garbage: an unrecognised status is far likelier to be drift or a
        bad write than a live build, and blocking on it recreates the one-way door."""
        assert bs.should_enqueue(_site("wat", age_seconds=1), TIMEOUT) is True


class TestSiteModelCarriesTheFields:
    def test_build_job_id_is_persisted_not_a_private_attr(self) -> None:
        """The DP0-4 mistake this fixes. ``_provision_job_id`` is a transient
        PrivateAttr, so a client that reloads loses its handle — and a queued build is
        exactly when a user reloads. Mutation: make it a PrivateAttr and this fails."""
        from pocketpaw_ee.cloud.models.site import Site

        assert "build_job_id" in Site.model_fields
        assert "build_status" in Site.model_fields
        assert "build_started_at" in Site.model_fields

    def test_build_fields_default_to_never_built(self) -> None:
        from pocketpaw_ee.cloud.models.site import Site

        assert Site.model_fields["build_status"].default == "none"
        assert Site.model_fields["build_started_at"].default is None
        assert Site.model_fields["build_job_id"].default is None

    def test_build_status_is_separate_from_provision_status(self) -> None:
        """A site is provisioned once and rebuilt many times; collapsing them would let
        a rebuild overwrite provisioning state."""
        from pocketpaw_ee.cloud.models.site import Site

        assert "provision_status" in Site.model_fields
        assert Site.model_fields["build_status"] is not Site.model_fields["provision_status"]


class TestTheWireCarriesTheState:
    """A queued build has to be visible to the client, or the concurrency cap turns a
    crash into a support ticket. These pin the wire contract, not the guard."""

    def test_site_response_exposes_build_status_and_job_id(self) -> None:
        from pocketpaw_ee.sites.dto import SiteResponse

        assert "build_status" in SiteResponse.model_fields
        assert "build_job_id" in SiteResponse.model_fields

    def test_wire_defaults_match_a_never_built_site(self) -> None:
        from pocketpaw_ee.sites.dto import SiteResponse

        assert SiteResponse.model_fields["build_status"].default == "none"
        assert SiteResponse.model_fields["build_job_id"].default is None

    def test_the_wire_vocabulary_matches_the_state_machine(self) -> None:
        """Drift between the model's states and the wire's is the kind of thing that
        ships a status the client has never heard of."""
        assert bs.IN_FLIGHT_STATUSES | bs.TERMINAL_STATUSES == {
            "none",
            "queued",
            "building",
            "built",
            "failed",
        }


class TestSettle:
    """SL-2 — what one attempt's verdict writes to the row.

    The load-bearing case is ``None``, not the happy path: a retryable failure with
    attempts left must stay IN FLIGHT, because ``should_enqueue`` reads any terminal
    status as free to re-publish. Writing ``failed`` between attempts invites a second
    sandbox on top of the retry.
    """

    def test_success_settles_as_built(self) -> None:
        assert bs.settle("completed_ok", retryable=False, attempts_left=0) == "built"

    def test_a_user_build_failure_settles_as_failed(self) -> None:
        assert bs.settle("build_failed", retryable=False, attempts_left=0) == "failed"

    def test_a_retryable_failure_with_attempts_left_stays_in_flight(self) -> None:
        # None, NOT "failed" — see the class docstring for why a transient terminal
        # status is worse than staying in flight.
        assert bs.settle("infra_lost", retryable=True, attempts_left=2) is None
        assert bs.settle("timed_out", retryable=True, attempts_left=1) is None

    def test_a_retryable_failure_with_no_attempts_left_settles_as_failed(self) -> None:
        # Today's real path: no attempt loop exists, so every caller passes 0.
        assert bs.settle("infra_lost", retryable=True, attempts_left=0) == "failed"
        assert bs.settle("timed_out", retryable=True, attempts_left=0) == "failed"

    def test_success_settles_even_with_attempts_left(self) -> None:
        """A success must never be held open by a retry budget it does not need."""
        assert bs.settle("completed_ok", retryable=True, attempts_left=5) == "built"

    def test_an_unrecognised_outcome_settles_as_failed_rather_than_retrying(self) -> None:
        """The opposite asymmetry to ``build_is_stale``, and deliberately so: an
        unknown status there reads as stale (act), because a stuck guard costs every
        future publish. Here an unknown outcome stops, because retrying something we
        cannot classify costs one sandbox per attempt without bound."""
        assert bs.settle("who_knows", retryable=False, attempts_left=0) == "failed"
        assert bs.settle("", retryable=False, attempts_left=3) == "failed"

    def test_every_settled_status_is_terminal(self) -> None:
        """The invariant tying this to the rest of the module: settle must never leave a
        row in an IN_FLIGHT status, or the build is finished and the guard still blocks."""
        for outcome in ("completed_ok", "build_failed", "timed_out", "infra_lost", "??"):
            for retryable in (True, False):
                got = bs.settle(outcome, retryable=retryable, attempts_left=0)
                assert got is not None
                assert got in bs.TERMINAL_STATUSES, (outcome, retryable, got)

    def test_the_reason_field_exists_to_carry_the_rung(self) -> None:
        """``settle`` names the status; the rung name rides ``build_reason``. Without
        that field a terminal failure cannot say whether the user's code broke or we
        lost the container — the two need opposite handling."""
        from pocketpaw_ee.cloud.models.site import Site

        assert "build_reason" in Site.model_fields
        assert Site.model_fields["build_reason"].default is None
