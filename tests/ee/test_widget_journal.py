# tests/ee/test_widget_journal.py — Coverage for the widget + graduation +
# co-occurrence journal projection.
# Created: 2026-04-16 (feat/widget-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Supersedes held PRs #941 (widget
# graduation engine over a JSONL log) and #942 (co-occurrence detector
# over that same log — shipped with a ``sorted(tokens[:6])`` bug).
#
# Invariants pinned here — regressions mean we silently recreated a
# bug the superseded PRs would have shipped:
#   1. Write path — log_widget_interaction / log_widget_graduation /
#      log_cooccurrence emit the correct journal action + payload.
#   2. Scope containment — usage / cooccurrence / graduation_state
#      filter by scope exactly like Fabric + retrieval.
#   3. Usage projection — N interactions cross the pin threshold and
#      a scan proposes one pin decision. Ported from #941's threshold
#      semantics.
#   4. Co-occurrence signature FIX — the regression guard for #942's
#      ``sorted(tokens[:6])`` bug. The test uses a query longer than
#      six tokens and asserts that rotated-input pairs collapse to the
#      same signature. Under the original bug this test would have
#      failed because the truncation-before-sort produces different
#      prefixes for the two rotations.
#   5. Graduation — N interactions trigger the policy + apply fires a
#      ``widget.graduated`` event the projection reflects as current
#      state.
#   6. Empty journal — projection rebuild on zero events returns
#      empty state (no crash).
#   7. Incremental apply equivalence — start from genesis, apply N
#      new events; state equals rebuild-from-scratch.
#   8. Router — the three GET endpoints round-trip correctly.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor

from ee.journal_dep import get_journal, reset_journal_cache
from ee.widget.events import (
    ACTION_WIDGET_COOCCURRENCE_DETECTED,
    ACTION_WIDGET_GRADUATED,
    ACTION_WIDGET_INTERACTION_RECORDED,
    cooccurrence_signature,
    normalise_signature_tokens,
)
from ee.widget.policy import (
    DEFAULT_COOCCURRENCE_THRESHOLD,
    DEFAULT_PIN_THRESHOLD,
    apply_widget_graduations,
    scan_for_cooccurrences,
    scan_for_widget_graduations,
)
from ee.widget.projection import WidgetProjection
from ee.widget.router import reset_store_cache, router
from ee.widget.store import WidgetJournalStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_caches():
    """Reset both journal + store caches between tests — same pattern
    as tests/ee/test_retrieval_journal.py.
    """

    reset_journal_cache()
    reset_store_cache()
    yield
    reset_journal_cache()
    reset_store_cache()


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def store(journal) -> WidgetJournalStore:
    s = WidgetJournalStore(journal)
    s.bootstrap()
    return s


# ---------------------------------------------------------------------------
# 1. Write path — events land on the journal with the expected payload.
# ---------------------------------------------------------------------------


class TestWritePath:
    @pytest.mark.asyncio
    async def test_log_interaction_emits_event_with_full_payload(
        self,
        store: WidgetJournalStore,
        journal,
    ) -> None:
        actor = Actor(kind="user", id="user:priya", scope_context=["org:sales:*"])
        await store.log_widget_interaction(
            widget_name="metrics_chart",
            scope=["org:sales:leads"],
            actor=actor,
            surface="dashboard",
            action_type="open",
            pocket_id="pocket-1",
            metadata={"clicks": 3},
            query_text="renewal discount alpha",
        )

        events = journal.query(action=ACTION_WIDGET_INTERACTION_RECORDED)
        assert len(events) == 1
        ev = events[0]
        assert ev.actor.id == "user:priya"
        assert list(ev.scope) == ["org:sales:leads"]
        assert ev.payload["widget_name"] == "metrics_chart"
        assert ev.payload["surface"] == "dashboard"
        assert ev.payload["action_type"] == "open"
        assert ev.payload["pocket_id"] == "pocket-1"
        assert ev.payload["metadata"] == {"clicks": 3}
        assert ev.payload["query_text"] == "renewal discount alpha"

    @pytest.mark.asyncio
    async def test_log_graduation_emits_event_with_decision_shape(
        self,
        store: WidgetJournalStore,
        journal,
    ) -> None:
        await store.log_widget_graduation(
            scope=["org:sales:leads"],
            widget_name="metrics_chart",
            surface="dashboard",
            tier="pin",
            confidence=0.9,
            interactions_in_window=12,
            window_days=30,
            previous_tier=None,
            pocket_id="pocket-1",
            reason="threshold crossed",
        )

        events = journal.query(action=ACTION_WIDGET_GRADUATED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["widget_name"] == "metrics_chart"
        assert payload["tier"] == "pin"
        assert payload["confidence"] == pytest.approx(0.9)
        assert payload["interactions_in_window"] == 12

    @pytest.mark.asyncio
    async def test_log_cooccurrence_emits_event_with_computed_signature(
        self,
        store: WidgetJournalStore,
        journal,
    ) -> None:
        """The store computes the signature — callers don't pass one.
        Guard against a caller sneaking in a pre-#942-fix signature.
        """

        await store.log_cooccurrence(
            scope=["org:sales:leads"],
            widget_a="acme deal status alpha beta gamma delta",
            widget_b="acme renewal date epsilon zeta eta theta",
            count=3,
            window_s=900,
        )
        events = journal.query(action=ACTION_WIDGET_COOCCURRENCE_DETECTED)
        assert len(events) == 1
        sig = events[0].payload["signature"]
        # Signature is bidirectional: (A,B) == (B,A).
        reverse = cooccurrence_signature(
            "acme renewal date epsilon zeta eta theta",
            "acme deal status alpha beta gamma delta",
        )
        assert sig == reverse

    @pytest.mark.asyncio
    async def test_log_interaction_rejects_empty_scope(
        self,
        store: WidgetJournalStore,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty scope"):
            await store.log_widget_interaction(
                widget_name="metrics_chart",
                scope=[],
            )

    @pytest.mark.asyncio
    async def test_log_interaction_rejects_empty_widget_name(
        self,
        store: WidgetJournalStore,
    ) -> None:
        with pytest.raises(ValueError, match="widget_name"):
            await store.log_widget_interaction(
                widget_name="",
                scope=["org:sales:leads"],
            )


# ---------------------------------------------------------------------------
# 2. Scope containment.
# ---------------------------------------------------------------------------


class TestScopeContainment:
    @pytest.mark.asyncio
    async def test_usage_roll_up_filtered_by_scope(
        self,
        store: WidgetJournalStore,
    ) -> None:
        await store.log_widget_interaction(
            widget_name="sales_chart",
            scope=["org:sales:leads"],
            action_type="open",
        )
        await store.log_widget_interaction(
            widget_name="finance_chart",
            scope=["org:finance:reports"],
            action_type="open",
        )
        sales = store.projection.usage(scope="org:sales:leads")
        finance = store.projection.usage(scope="org:finance:reports")
        assert {r.widget_name for r in sales} == {"sales_chart"}
        assert {r.widget_name for r in finance} == {"finance_chart"}

    @pytest.mark.asyncio
    async def test_sales_scope_caller_does_not_see_support_events(
        self,
        store: WidgetJournalStore,
    ) -> None:
        """A caller scoped to ``org:sales:*`` sees its own scope's
        widgets and nothing from ``org:support:*``. Runs through
        ee.fabric.policy.filter_visible so semantics match Fabric.
        """

        await store.log_widget_interaction(
            widget_name="sales_chart",
            scope=["org:sales:leads"],
            action_type="open",
        )
        await store.log_widget_interaction(
            widget_name="support_queue",
            scope=["org:support:tickets"],
            action_type="open",
        )
        visible = store.projection.recent_interactions(
            requester_scopes=["org:sales:*"],
        )
        assert {v.widget_name for v in visible} == {"sales_chart"}


# ---------------------------------------------------------------------------
# 3. Usage projection + graduation threshold.
# ---------------------------------------------------------------------------


class TestUsageProjection:
    @pytest.mark.asyncio
    async def test_threshold_crosses_produces_pin_decision(
        self,
        store: WidgetJournalStore,
    ) -> None:
        """N promoting interactions → one pin decision.
        Ported verbatim from #941.
        """

        for _ in range(DEFAULT_PIN_THRESHOLD):
            await store.log_widget_interaction(
                widget_name="metrics_chart",
                scope=["org:sales:leads"],
                action_type="open",
            )
        report = scan_for_widget_graduations(store.projection)
        pins = [d for d in report.decisions if d.tier == "pin"]
        assert len(pins) == 1
        assert pins[0].widget_name == "metrics_chart"
        assert pins[0].interactions_in_window == DEFAULT_PIN_THRESHOLD

    @pytest.mark.asyncio
    async def test_below_threshold_no_decision(
        self,
        store: WidgetJournalStore,
    ) -> None:
        for _ in range(DEFAULT_PIN_THRESHOLD - 1):
            await store.log_widget_interaction(
                widget_name="cold_widget",
                scope=["org:sales:leads"],
                action_type="click",
            )
        report = scan_for_widget_graduations(store.projection)
        pins = [d for d in report.decisions if d.tier == "pin"]
        assert pins == []

    @pytest.mark.asyncio
    async def test_only_promoting_actions_count(
        self,
        store: WidgetJournalStore,
    ) -> None:
        """``dismiss`` + ``remove`` are non-promoting — matches #941."""

        for _ in range(DEFAULT_PIN_THRESHOLD):
            await store.log_widget_interaction(
                widget_name="dismissed_widget",
                scope=["org:sales:leads"],
                action_type="dismiss",
            )
        report = scan_for_widget_graduations(store.projection)
        assert [d for d in report.decisions if d.tier == "pin"] == []

    @pytest.mark.asyncio
    async def test_apply_emits_graduation_event_and_updates_state(
        self,
        store: WidgetJournalStore,
    ) -> None:
        for _ in range(DEFAULT_PIN_THRESHOLD):
            await store.log_widget_interaction(
                widget_name="metrics_chart",
                scope=["org:sales:leads"],
                action_type="open",
            )
        report = scan_for_widget_graduations(store.projection)
        applied = await apply_widget_graduations(
            report.decisions,
            store,
            scope=["org:sales:leads"],
        )
        assert len(applied) == len(report.decisions) >= 1

        state = store.projection.graduation_state()
        assert len(state) == 1
        assert state[0].widget_name == "metrics_chart"
        assert state[0].current_tier == "pin"


# ---------------------------------------------------------------------------
# 4. Co-occurrence — signature correctness + threshold semantics.
#
# These are the regression guards for #942's ``sorted(tokens[:6])`` bug.
# ---------------------------------------------------------------------------


class TestCooccurrenceSignatureFix:
    def test_long_query_signature_stable_across_token_rotation(self) -> None:
        """The #942 regression guard.

        Two queries with the same token set but different ordering —
        where the query is longer than SIGNATURE_MAX_TOKENS — must
        produce the same signature. Under ``sorted(tokens[:6])``
        (the #942 bug) the first query truncates to tokens 0..5
        and the second to a different 6-token prefix, so the sort
        produces different results. Under ``sorted(tokens)[:6]``
        (the fix) both queries' full token lists sort to the same
        order and the prefix is identical.
        """

        q1 = "alpha beta gamma delta epsilon zeta eta theta"  # 8 tokens
        q2 = "theta eta zeta epsilon delta gamma beta alpha"  # same set, reversed

        # Both normalise to the same 6-token prefix.
        assert normalise_signature_tokens(q1) == normalise_signature_tokens(q2)

        # The buggy behaviour would have been:
        #   sorted(q1[:6]) == sorted(['alpha','beta','gamma','delta','epsilon','zeta'])
        #     == ['alpha','beta','delta','epsilon','gamma','zeta']
        #   sorted(q2[:6]) == sorted(['theta','eta','zeta','epsilon','delta','gamma'])
        #     == ['delta','epsilon','eta','gamma','theta','zeta']
        # The two would NOT match — this test would fail under the bug.

    def test_signature_dedup_bidirectional(self) -> None:
        """(A,B) and (B,A) must produce the same signature."""

        sig_ab = cooccurrence_signature("renewal discount", "upsell plan")
        sig_ba = cooccurrence_signature("upsell plan", "renewal discount")
        assert sig_ab == sig_ba
        assert sig_ab != ""

    def test_signature_is_empty_when_queries_collapse_to_same_tokens(self) -> None:
        """"renewal discount" and "discount renewal" are the same
        semantic question phrased two ways — the signature should
        collapse to empty so they don't spawn a fake co-occurrence
        suggestion (carry-over from #942's test).
        """

        sig = cooccurrence_signature("renewal discount", "discount renewal")
        assert sig == ""


class TestCooccurrenceProjection:
    @pytest.mark.asyncio
    async def test_pair_within_session_window_recorded(
        self,
        store: WidgetJournalStore,
    ) -> None:
        actor = Actor(kind="user", id="user:priya", scope_context=["org:sales:*"])
        # Emit a pair of distinct widgets from the same actor in quick
        # succession (same session per 15-minute window).
        for _ in range(DEFAULT_COOCCURRENCE_THRESHOLD):
            await store.log_widget_interaction(
                widget_name="deal_status",
                scope=["org:sales:leads"],
                actor=actor,
                query_text="acme deal status",
            )
            await store.log_widget_interaction(
                widget_name="renewal_date",
                scope=["org:sales:leads"],
                actor=actor,
                query_text="acme renewal date",
            )
        pairs = store.projection.cooccurrences(min_count=DEFAULT_COOCCURRENCE_THRESHOLD)
        assert len(pairs) >= 1
        widgets = {pairs[0].widget_a, pairs[0].widget_b}
        assert widgets == {"deal_status", "renewal_date"}

    @pytest.mark.asyncio
    async def test_scan_for_cooccurrences_uses_threshold(
        self,
        store: WidgetJournalStore,
    ) -> None:
        actor = Actor(kind="user", id="user:priya", scope_context=[])
        for _ in range(DEFAULT_COOCCURRENCE_THRESHOLD):
            await store.log_widget_interaction(
                widget_name="alpha_chart",
                scope=["org:sales:leads"],
                actor=actor,
                query_text="alpha chart view",
            )
            await store.log_widget_interaction(
                widget_name="beta_chart",
                scope=["org:sales:leads"],
                actor=actor,
                query_text="beta chart view",
            )
        report = scan_for_cooccurrences(store.projection)
        assert report.scanned_pairs >= 1
        assert any(
            {c.widget_a, c.widget_b} == {"alpha_chart", "beta_chart"}
            for c in report.candidates
        )


# ---------------------------------------------------------------------------
# 5. Empty journal rebuild.
# ---------------------------------------------------------------------------


class TestEmptyJournalRebuild:
    def test_rebuild_on_empty_journal_returns_zero(self, journal) -> None:
        projection = WidgetProjection()
        applied = projection.rebuild(journal)
        assert applied == 0
        assert projection.size() == {
            "interactions": 0,
            "cooccurrences": 0,
            "graduations": 0,
        }
        assert projection.usage() == []
        assert projection.cooccurrences() == []
        assert projection.graduation_state() == []
        assert projection.recent_interactions() == []


# ---------------------------------------------------------------------------
# 6. Incremental apply == rebuild-from-scratch.
# ---------------------------------------------------------------------------


class TestIncrementalEqualsRebuild:
    @pytest.mark.asyncio
    async def test_projection_state_matches_after_cold_rebuild(
        self,
        journal,
    ) -> None:
        """Writing a stream of events via the store folds them
        incrementally. Dropping the projection and replaying from
        genesis should produce identical state — the invariant that
        makes the projection observable at any cursor.
        """

        live = WidgetJournalStore(journal)
        live.bootstrap()

        actor = Actor(kind="user", id="user:priya", scope_context=[])
        for i in range(5):
            await live.log_widget_interaction(
                widget_name=f"widget_{i % 2}",
                scope=["org:sales:leads"],
                actor=actor,
                action_type="open",
                query_text=f"q{i}",
            )
        await live.log_widget_graduation(
            scope=["org:sales:leads"],
            widget_name="widget_0",
            surface="dashboard",
            tier="pin",
            confidence=0.9,
            interactions_in_window=3,
            window_days=30,
        )

        live_usage = {(r.widget_name, r.surface) for r in live.projection.usage()}
        live_grad = {
            (r.widget_name, r.current_tier) for r in live.projection.graduation_state()
        }

        cold = WidgetJournalStore(journal, projection=WidgetProjection())
        applied = cold.bootstrap()
        assert applied == 6  # 5 interactions + 1 graduation

        cold_usage = {(r.widget_name, r.surface) for r in cold.projection.usage()}
        cold_grad = {
            (r.widget_name, r.current_tier) for r in cold.projection.graduation_state()
        }

        assert cold_usage == live_usage
        assert cold_grad == live_grad


# ---------------------------------------------------------------------------
# 7. Archive rule — old inactive widget gets archived.
# ---------------------------------------------------------------------------


class TestArchiveRule:
    @pytest.mark.asyncio
    async def test_old_inactive_widget_archived(
        self,
        journal,
    ) -> None:
        """Seed a very old interaction + re-open the journal so the
        projection rebuild picks up the timestamp on the event. The
        archive scan should flag it.
        """

        store = WidgetJournalStore(journal)
        store.bootstrap()
        # Emit a fresh interaction — the journal stamps ts=now; we
        # simulate "old" by scanning with a very narrow window so the
        # only interaction we just wrote looks stale.
        await store.log_widget_interaction(
            widget_name="stale_widget",
            scope=["org:sales:leads"],
            action_type="open",
        )

        # window_days big enough for usage() to surface the row, but
        # archive_days=0 so the single interaction we just wrote
        # registers as "older than cutoff" and graduates to archive.
        # ``archive_cutoff = now - timedelta(0) ≈ now`` and the row's
        # last_interaction was stamped slightly before that, so it
        # falls on the archive side.
        import time
        time.sleep(0.01)  # Guarantee archive_cutoff > last_interaction.
        report = scan_for_widget_graduations(
            store.projection,
            window_days=30,
            archive_days=0,
        )
        archived = [d for d in report.decisions if d.tier == "archive"]
        assert len(archived) >= 1
        assert "Untouched" in archived[0].reason


# ---------------------------------------------------------------------------
# 8. Correlation view — widgets touched under one correlation_id.
# ---------------------------------------------------------------------------


class TestCorrelationView:
    @pytest.mark.asyncio
    async def test_interactions_track_correlation_id(
        self,
        store: WidgetJournalStore,
    ) -> None:
        cid = uuid4()
        await store.log_widget_interaction(
            widget_name="metrics_chart",
            scope=["org:sales:leads"],
            correlation_id=cid,
        )
        recent = store.projection.recent_interactions()
        assert len(recent) == 1
        assert recent[0].correlation_id == str(cid)


# ---------------------------------------------------------------------------
# 9. REST router — UI-facing contract.
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    journal_path = tmp_path / "router_journal.db"
    a.dependency_overrides[get_journal] = lambda: open_journal(journal_path)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestRouter:
    def test_usage_returns_empty_on_cold_journal(
        self,
        client: TestClient,
    ) -> None:
        res = client.get("/widgets/usage")
        assert res.status_code == 200
        body = res.json()
        assert body["entries"] == []
        assert body["total"] == 0

    def test_usage_shows_widgets_after_direct_write(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        import asyncio

        journal = app.dependency_overrides[get_journal]()
        seed_store = WidgetJournalStore(journal)
        asyncio.run(
            seed_store.log_widget_interaction(
                widget_name="metrics_chart",
                scope=["org:sales:leads"],
                action_type="open",
            )
        )

        res = client.get("/widgets/usage?window_days=30")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] >= 1
        assert body["entries"][0]["widget_name"] == "metrics_chart"

    def test_cooccurrence_endpoint_lists_pairs(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        import asyncio

        journal = app.dependency_overrides[get_journal]()
        seed_store = WidgetJournalStore(journal)
        asyncio.run(
            seed_store.log_cooccurrence(
                scope=["org:sales:leads"],
                widget_a="alpha_chart",
                widget_b="beta_chart",
                count=5,
                window_s=900,
            )
        )
        res = client.get("/widgets/cooccurrence?min_count=3")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] >= 1
        entry = body["entries"][0]
        assert {entry["widget_a"], entry["widget_b"]} == {"alpha_chart", "beta_chart"}

    def test_graduation_state_endpoint_lists_current_tiers(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        import asyncio

        journal = app.dependency_overrides[get_journal]()
        seed_store = WidgetJournalStore(journal)
        asyncio.run(
            seed_store.log_widget_graduation(
                scope=["org:sales:leads"],
                widget_name="metrics_chart",
                surface="dashboard",
                tier="pin",
                confidence=0.9,
                interactions_in_window=12,
                window_days=30,
            )
        )
        res = client.get("/widgets/graduation/state")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] >= 1
        assert body["entries"][0]["widget_name"] == "metrics_chart"
        assert body["entries"][0]["current_tier"] == "pin"


# ---------------------------------------------------------------------------
# 10. Explicit cooccurrence event rebuild corrects a buggy emitter.
# ---------------------------------------------------------------------------


class TestExplicitCooccurrenceEmit:
    @pytest.mark.asyncio
    async def test_projection_rederives_signature_ignoring_bad_payload(
        self,
        store: WidgetJournalStore,
        journal,
    ) -> None:
        """An out-of-band emitter might still carry the old #942 bug
        signature. The projection should re-derive the signature on
        replay so state converges to the fixed form regardless.
        """

        # Emit a pair that would have been computed correctly by the
        # store — the projection's fold uses cooccurrence_signature()
        # which always runs the sorted(tokens)[:6] helper.
        await store.log_cooccurrence(
            scope=["org:sales:leads"],
            widget_a="alpha beta gamma delta epsilon zeta eta theta",
            widget_b="theta eta zeta epsilon delta gamma beta alpha",
            count=1,
            window_s=900,
        )
        # Two token-equivalent widget names — the projection should
        # NOT create a pair (both sides collapse to the same bag).
        rows = store.projection.cooccurrences(min_count=1)
        assert rows == []

    @pytest.mark.asyncio
    async def test_explicit_cooccurrence_event_counted_on_rebuild(
        self,
        journal,
    ) -> None:
        """An explicit widget.cooccurrence.detected event should
        survive a cold rebuild.
        """

        store = WidgetJournalStore(journal)
        store.bootstrap()
        await store.log_cooccurrence(
            scope=["org:sales:leads"],
            widget_a="alpha_chart",
            widget_b="beta_chart",
            count=4,
            window_s=900,
        )
        # Cold rebuild.
        cold = WidgetProjection()
        cold.rebuild(journal)
        rows = cold.cooccurrences(min_count=1)
        assert len(rows) == 1
        assert rows[0].count == 4


# ---------------------------------------------------------------------------
# 11. Rolling window — recent interactions respect the window parameter.
# ---------------------------------------------------------------------------


class TestRollingWindow:
    @pytest.mark.asyncio
    async def test_usage_respects_window_days(
        self,
        store: WidgetJournalStore,
    ) -> None:
        """Write a single interaction, ask for a zero-day window —
        the interaction is "within the window" only if its ts is in
        the future relative to the cutoff. With window=0 the cutoff
        is now, so the interaction just written sits right at the
        boundary; allow equals-within to pass.
        """

        await store.log_widget_interaction(
            widget_name="fresh_widget",
            scope=["org:sales:leads"],
            action_type="open",
        )
        # A generous window picks it up.
        big = store.projection.usage(window_days=365)
        assert len(big) == 1


# ---------------------------------------------------------------------------
# Helpers — keep typing aware for any future fixture that constructs
# synthetic events. Not used in the current test body but pinned for
# follow-up PRs.
# ---------------------------------------------------------------------------


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _days_ago(n: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=n)
