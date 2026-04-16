# tests/ee/test_retrieval_journal.py — Coverage for the retrieval + graduation
# journal projection.
# Created: 2026-04-16 (feat/retrieval-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Supersedes the held PRs #936 (JSONL retrieval
# sink) and #937 (graduation policy over that JSONL).
#
# These tests pin the invariants the projection-based design is supposed to
# hold. If any regress we've silently recreated the bugs that held those PRs:
#   1. Write path — ``log_retrieval`` / ``log_graduation`` emit the expected
#      journal events with the full payload shape.
#   2. Scope filter — ``recent_retrievals(scope=...)`` returns only
#      scope-matching events; cross-scope readers see an empty list.
#   3. Correlation view — ``retrievals_by_correlation`` groups events from
#      one run chronologically.
#   4. Graduation projection — N retrievals of the same candidate cross the
#      threshold + emit a decision; applying the decision writes a
#      ``graduation.applied`` event visible via the projection.
#   5. Projection rebuild on empty journal returns empty state (no crash).
#   6. Incremental apply — projection state after one incremental event
#      equals the rebuild-from-scratch state.
#   7. REST router — GET /retrieval/recent returns the expected envelope,
#      GET /graduation/state returns the current per-memory decisions.

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor

from ee.journal_dep import get_journal, reset_journal_cache
from ee.retrieval.events import (
    ACTION_GRADUATION_APPLIED,
    ACTION_RETRIEVAL_QUERY,
)
from ee.retrieval.policy import (
    DEFAULT_EPISODIC_THRESHOLD,
    DEFAULT_SEMANTIC_THRESHOLD,
    apply_decisions,
    scan_for_graduations,
)
from ee.retrieval.projection import RetrievalProjection
from ee.retrieval.router import reset_store_cache, router
from ee.retrieval.store import RetrievalJournalStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_caches():
    """Reset both journal + store caches between tests.

    The journal dep has an ``lru_cache`` and the router caches one store
    per Journal id; without resets, a prior test's state leaks forward.
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
def store(journal) -> RetrievalJournalStore:
    s = RetrievalJournalStore(journal)
    s.bootstrap()
    return s


def _candidate(
    memory_id: str,
    *,
    source: str = "soul",
    tier: str = "episodic",
    score: float = 0.5,
) -> dict:
    return {"id": memory_id, "source": source, "tier": tier, "score": score}


# ---------------------------------------------------------------------------
# 1. Write path — events land on the journal with the expected payload.
# ---------------------------------------------------------------------------


class TestWritePath:
    @pytest.mark.asyncio
    async def test_log_retrieval_emits_event_with_full_payload(
        self,
        store: RetrievalJournalStore,
        journal,
    ) -> None:
        """One log_retrieval call should surface as exactly one
        ``retrieval.query`` event with every field propagated into the
        journal payload — nothing dropped, nothing fabricated."""

        actor = Actor(kind="user", id="user:priya", scope_context=["org:sales:*"])
        await store.log_retrieval(
            scope=["org:sales:leads"],
            query="renewal discount",
            strategy="parallel",
            sources_queried=["soul", "kb"],
            candidates=[_candidate("mem_x")],
            picked=["mem_x"],
            latency_ms=12,
            pocket_id="pocket-1",
            actor=actor,
        )

        events = journal.query(action=ACTION_RETRIEVAL_QUERY)
        assert len(events) == 1
        event = events[0]
        assert event.actor.id == "user:priya"
        assert list(event.scope) == ["org:sales:leads"]
        assert event.payload["query"] == "renewal discount"
        assert event.payload["strategy"] == "parallel"
        assert event.payload["sources_queried"] == ["soul", "kb"]
        assert event.payload["candidate_count"] == 1
        assert event.payload["candidates"][0]["id"] == "mem_x"
        assert event.payload["picked"] == ["mem_x"]
        assert event.payload["latency_ms"] == 12
        assert event.payload["pocket_id"] == "pocket-1"

    @pytest.mark.asyncio
    async def test_log_graduation_emits_event_with_decision_shape(
        self,
        store: RetrievalJournalStore,
        journal,
    ) -> None:
        await store.log_graduation(
            scope=["org:sales:leads"],
            memory_id="mem_x",
            kind="episodic_to_semantic",
            access_count=12,
            window_days=30,
            from_tier="episodic",
            to_tier="semantic",
            pocket_id="pocket-1",
            reason="threshold crossed",
        )

        events = journal.query(action=ACTION_GRADUATION_APPLIED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["memory_id"] == "mem_x"
        assert payload["kind"] == "episodic_to_semantic"
        assert payload["access_count"] == 12
        assert payload["from_tier"] == "episodic"
        assert payload["to_tier"] == "semantic"

    @pytest.mark.asyncio
    async def test_log_retrieval_rejects_empty_scope(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        """The journal's EventEntry refuses scope=[]. Surface the
        violation at the store boundary so the caller sees a clear
        pocketpaw error instead of a pydantic validator trace."""

        with pytest.raises(ValueError, match="non-empty scope"):
            await store.log_retrieval(scope=[], query="x")

    @pytest.mark.asyncio
    async def test_log_graduation_rejects_empty_scope(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty scope"):
            await store.log_graduation(
                scope=[],
                memory_id="mem_x",
                kind="episodic_to_semantic",
                access_count=12,
                window_days=30,
                from_tier="episodic",
                to_tier="semantic",
            )


# ---------------------------------------------------------------------------
# 2. Scope-filtered recent retrievals.
# ---------------------------------------------------------------------------


class TestScopeFilter:
    @pytest.mark.asyncio
    async def test_recent_retrievals_filtered_by_scope(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        """recent_retrievals(scope=X) returns only entries tagged with
        exactly that scope on the event."""

        await store.log_retrieval(
            scope=["org:sales:leads"],
            query="sales q",
            candidates=[_candidate("mem_a")],
        )
        await store.log_retrieval(
            scope=["org:finance:reports"],
            query="finance q",
            candidates=[_candidate("mem_b")],
        )

        sales = store.projection.recent_retrievals(scope="org:sales:leads")
        assert len(sales) == 1
        assert sales[0].query == "sales q"

        finance = store.projection.recent_retrievals(scope="org:finance:reports")
        assert len(finance) == 1
        assert finance[0].query == "finance q"

    @pytest.mark.asyncio
    async def test_recent_retrievals_filtered_by_actor(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        await store.log_retrieval(
            scope=["org:sales:leads"],
            query="q1",
            actor=Actor(kind="user", id="user:priya"),
        )
        await store.log_retrieval(
            scope=["org:sales:leads"],
            query="q2",
            actor=Actor(kind="user", id="user:maya"),
        )

        priya = store.projection.recent_retrievals(actor_id="user:priya")
        assert len(priya) == 1
        assert priya[0].query == "q1"

    @pytest.mark.asyncio
    async def test_recent_retrievals_applies_requester_scope_containment(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        """A caller scoped to ``org:sales:*`` sees their own events and
        nothing from ``org:finance:*``. Uses the same policy engine as
        Fabric so the containment rules stay identical."""

        await store.log_retrieval(
            scope=["org:sales:leads"],
            query="sales",
            candidates=[_candidate("mem_a")],
        )
        await store.log_retrieval(
            scope=["org:finance:reports"],
            query="finance",
            candidates=[_candidate("mem_b")],
        )

        visible = store.projection.recent_retrievals(
            requester_scopes=["org:sales:*"],
        )
        assert len(visible) == 1
        assert visible[0].query == "sales"


# ---------------------------------------------------------------------------
# 3. Correlation view — retrievals in one "session".
# ---------------------------------------------------------------------------


class TestCorrelationView:
    @pytest.mark.asyncio
    async def test_retrievals_by_correlation_returns_events_in_order(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        cid = uuid4()
        await store.log_retrieval(
            scope=["org:sales:leads"],
            query="first",
            correlation_id=cid,
        )
        await store.log_retrieval(
            scope=["org:sales:leads"],
            query="second",
            correlation_id=cid,
        )
        await store.log_retrieval(
            scope=["org:sales:leads"],
            query="other session",
            correlation_id=uuid4(),
        )

        rows = store.projection.retrievals_by_correlation(str(cid))
        assert [r.query for r in rows] == ["first", "second"]


# ---------------------------------------------------------------------------
# 4. Graduation policy + apply over the projection.
# ---------------------------------------------------------------------------


class TestGraduationPolicy:
    @pytest.mark.asyncio
    async def test_threshold_crosses_produces_decision(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        """N retrievals (N == episodic threshold) where every trace lists
        the same memory_id as a candidate should produce one
        episodic→semantic decision. Ported from #937."""

        for _ in range(DEFAULT_EPISODIC_THRESHOLD):
            await store.log_retrieval(
                scope=["org:sales:leads"],
                query="q",
                candidates=[_candidate("mem_hot")],
            )

        report = scan_for_graduations(store.projection)
        assert len(report.decisions) == 1
        d = report.decisions[0]
        assert d.memory_id == "mem_hot"
        assert d.kind == "episodic_to_semantic"
        assert d.access_count == DEFAULT_EPISODIC_THRESHOLD
        assert d.from_tier == "episodic"
        assert d.to_tier == "semantic"

    @pytest.mark.asyncio
    async def test_below_threshold_yields_no_decision(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        for _ in range(DEFAULT_EPISODIC_THRESHOLD - 1):
            await store.log_retrieval(
                scope=["org:sales:leads"],
                query="q",
                candidates=[_candidate("mem_cold")],
            )
        report = scan_for_graduations(store.projection)
        assert report.decisions == []

    @pytest.mark.asyncio
    async def test_semantic_threshold_promotes_to_core(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        for _ in range(DEFAULT_SEMANTIC_THRESHOLD):
            await store.log_retrieval(
                scope=["org:sales:leads"],
                query="q",
                candidates=[_candidate("mem_core", tier="semantic")],
            )
        report = scan_for_graduations(store.projection)
        decisions = [d for d in report.decisions if d.memory_id == "mem_core"]
        assert len(decisions) == 1
        assert decisions[0].kind == "semantic_to_core"

    @pytest.mark.asyncio
    async def test_apply_decisions_emits_graduation_event(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        """apply_decisions should write one ``graduation.applied`` event
        per decision and surface the result in the projection's
        ``graduation_state`` view."""

        for _ in range(DEFAULT_EPISODIC_THRESHOLD):
            await store.log_retrieval(
                scope=["org:sales:leads"],
                query="q",
                candidates=[_candidate("mem_hot")],
            )

        report = scan_for_graduations(store.projection)
        applied = await apply_decisions(
            report.decisions,
            store,
            scope=["org:sales:leads"],
        )
        assert len(applied) == len(report.decisions) == 1

        state = store.projection.graduation_state()
        assert len(state) == 1
        assert state[0].memory_id == "mem_hot"
        assert state[0].current_tier == "semantic"
        assert state[0].previous_tier == "episodic"

    @pytest.mark.asyncio
    async def test_apply_decisions_skipped_when_soul_missing(
        self,
        store: RetrievalJournalStore,
    ) -> None:
        """Journal emission must succeed even when no soul is supplied —
        the soul mutation is best-effort per #937."""

        from ee.retrieval.policy import GraduationDecision

        decision = GraduationDecision(
            memory_id="mem_manual",
            kind="episodic_to_semantic",
            access_count=15,
            window_days=30,
            from_tier="episodic",
            to_tier="semantic",
        )
        applied = await apply_decisions(
            [decision],
            store,
            scope=["org:sales:leads"],
            soul=None,
        )
        assert len(applied) == 1


# ---------------------------------------------------------------------------
# 5. Empty journal rebuild — no crash.
# ---------------------------------------------------------------------------


class TestEmptyJournalRebuild:
    def test_rebuild_on_empty_journal_returns_zero(self, journal) -> None:
        projection = RetrievalProjection()
        applied = projection.rebuild(journal)
        assert applied == 0
        assert projection.size() == {"retrievals": 0, "graduations": 0}
        assert projection.recent_retrievals() == []
        assert projection.graduation_state() == []


# ---------------------------------------------------------------------------
# 6. Incremental apply == rebuild-from-scratch.
# ---------------------------------------------------------------------------


class TestIncrementalEqualsRebuild:
    @pytest.mark.asyncio
    async def test_projection_state_matches_after_cold_rebuild(
        self,
        journal,
    ) -> None:
        """Writing a sequence of events via the store (which folds
        incrementally) should produce identical projection state to
        dropping the projection and replaying from genesis."""

        live = RetrievalJournalStore(journal)
        live.bootstrap()

        for i in range(5):
            await live.log_retrieval(
                scope=["org:sales:leads"],
                query=f"q{i}",
                candidates=[_candidate(f"mem_{i}")],
            )
        await live.log_graduation(
            scope=["org:sales:leads"],
            memory_id="mem_0",
            kind="episodic_to_semantic",
            access_count=11,
            window_days=30,
            from_tier="episodic",
            to_tier="semantic",
        )

        live_retr = {v.query for v in live.projection.recent_retrievals(limit=100)}
        live_grad = {r.memory_id for r in live.projection.graduation_state()}

        cold = RetrievalJournalStore(journal, projection=RetrievalProjection())
        applied = cold.bootstrap()
        assert applied == 6  # 5 retrievals + 1 graduation

        cold_retr = {v.query for v in cold.projection.recent_retrievals(limit=100)}
        cold_grad = {r.memory_id for r in cold.projection.graduation_state()}

        assert cold_retr == live_retr
        assert cold_grad == live_grad


# ---------------------------------------------------------------------------
# 7. REST router — the UI-facing contract.
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """FastAPI app with the retrieval router + get_journal overridden to
    a tmp-path journal. Matches the fleet router's dep-override pattern.
    """

    a = FastAPI()
    a.include_router(router)
    journal_path = tmp_path / "router_journal.db"
    a.dependency_overrides[get_journal] = lambda: open_journal(journal_path)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestRouter:
    def test_recent_returns_empty_envelope_on_cold_journal(
        self,
        client: TestClient,
    ) -> None:
        res = client.get("/retrieval/recent")
        assert res.status_code == 200
        body = res.json()
        assert body == {"entries": [], "total": 0}

    def test_recent_shows_retrievals_after_direct_write(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        """Seed the journal via get_journal()'s override, then GET
        /retrieval/recent — the warmed store should pick up the event on
        first call via bootstrap()."""

        # Write directly to the journal the override hands out.
        journal = app.dependency_overrides[get_journal]()
        seed_store = RetrievalJournalStore(journal)
        import asyncio

        asyncio.run(
            seed_store.log_retrieval(
                scope=["org:sales:leads"],
                query="recent q",
                candidates=[_candidate("mem_x")],
            )
        )

        res = client.get("/retrieval/recent?limit=10")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["entries"][0]["query"] == "recent q"
        assert body["entries"][0]["scope"] == ["org:sales:leads"]

    def test_recent_filters_by_scope_query_param(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        journal = app.dependency_overrides[get_journal]()
        seed_store = RetrievalJournalStore(journal)
        import asyncio

        asyncio.run(
            seed_store.log_retrieval(
                scope=["org:sales:leads"],
                query="sales",
            )
        )
        asyncio.run(
            seed_store.log_retrieval(
                scope=["org:finance:reports"],
                query="finance",
            )
        )

        res = client.get("/retrieval/recent?scope=org%3Asales%3Aleads")
        body = res.json()
        assert body["total"] == 1
        assert body["entries"][0]["query"] == "sales"

    def test_session_endpoint_returns_404_when_missing(
        self,
        client: TestClient,
    ) -> None:
        res = client.get(f"/retrieval/session/{uuid4()}")
        assert res.status_code == 404

    def test_graduation_state_endpoint_lists_current_decisions(
        self,
        client: TestClient,
        app: FastAPI,
    ) -> None:
        journal = app.dependency_overrides[get_journal]()
        seed_store = RetrievalJournalStore(journal)
        import asyncio

        asyncio.run(
            seed_store.log_graduation(
                scope=["org:sales:leads"],
                memory_id="mem_hot",
                kind="episodic_to_semantic",
                access_count=12,
                window_days=30,
                from_tier="episodic",
                to_tier="semantic",
            )
        )

        res = client.get("/graduation/state")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["entries"][0]["memory_id"] == "mem_hot"
        assert body["entries"][0]["current_tier"] == "semantic"
