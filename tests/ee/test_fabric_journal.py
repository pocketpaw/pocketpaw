# tests/ee/test_fabric_journal.py — Coverage for the journal-backed Fabric slice.
# Created: 2026-04-16 (feat/fabric-journal-projection) — Wave 3 / Org Architecture
# RFC, Phase 3. Supersedes #938.
#
# These tests pin the four invariants the projection-based design is supposed to
# hold. If any of them regress we've silently recreated #938's bugs:
#   1. Happy-path lifecycle — create → query → update → query → archive → query.
#   2. Scope filter — a cross-scope query returns 0, not 404, and never reveals
#      the denied entity's existence.
#   3. Pagination correctness — `total` is post-filter, never pre-filter. This is
#      the exact leak #938 couldn't close.
#   4. Projection rebuild — wipe in-memory state, replay from genesis, end up
#      with identical state.
#
# Two additional tests cover incremental apply (single-event delta after a rebuild)
# and the scope-required-on-write invariant.

from __future__ import annotations

from pathlib import Path

import pytest
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor

from ee.fabric.events import ACTION_OBJECT_CREATED
from ee.fabric.journal_store import FabricJournalStore
from ee.fabric.models import FabricObject, FabricQuery
from ee.fabric.policy import PolicyDecision, decide, filter_visible, visible
from ee.fabric.projection import FabricProjection


@pytest.fixture
def journal(tmp_path: Path):
    """Open a throwaway journal per test. ``open_journal`` creates the
    backing SQLite file on first append, so this fixture is essentially
    free when a test doesn't write anything.
    """

    j = open_journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def store(journal) -> FabricJournalStore:
    s = FabricJournalStore(journal)
    s.bootstrap()
    return s


def _obj(type_id: str = "t1", type_name: str = "Customer", **props) -> FabricObject:
    return FabricObject(
        type_id=type_id,
        type_name=type_name,
        properties=props or {},
    )


# ---------------------------------------------------------------------------
# 1. Happy-path lifecycle
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_create_query_update_query_archive_query(
        self,
        store: FabricJournalStore,
    ) -> None:
        """End-to-end: a sales-scoped caller should see their object
        appear, reflect an update, and disappear on archive."""

        scope = ["org:sales:leads"]
        obj = _obj(name="Acme", revenue=10_000)
        created = await store.create(obj, scope=scope)
        assert created.id == obj.id
        assert created.properties == {"name": "Acme", "revenue": 10_000}

        result = await store.query(FabricQuery(), requester_scopes=scope)
        assert result.total == 1
        assert result.objects[0].id == obj.id

        updated = await store.update(obj.id, {"revenue": 25_000}, scope=scope)
        assert updated is not None
        assert updated.properties["revenue"] == 25_000
        # Merge semantics — existing keys survive the update.
        assert updated.properties["name"] == "Acme"

        result2 = await store.query(FabricQuery(), requester_scopes=scope)
        assert result2.total == 1
        assert result2.objects[0].properties["revenue"] == 25_000

        gone = await store.archive(obj.id, scope=scope)
        assert gone is True

        result3 = await store.query(FabricQuery(), requester_scopes=scope)
        assert result3.total == 0
        assert result3.objects == []


# ---------------------------------------------------------------------------
# 2. Scope filter
# ---------------------------------------------------------------------------


class TestScopeFilter:
    @pytest.mark.asyncio
    async def test_cross_scope_query_returns_empty_not_error(
        self,
        store: FabricJournalStore,
    ) -> None:
        """A support caller looking at a sales-scoped object sees an
        empty result set — not a 404, not an error, not a count leak.
        The filter is indistinguishable from 'no data exists'."""

        await store.create(_obj(name="Lead A"), scope=["org:sales:leads"])

        result = await store.query(
            FabricQuery(),
            requester_scopes=["org:support:*"],
        )
        assert result.total == 0
        assert result.objects == []

    @pytest.mark.asyncio
    async def test_matching_scope_sees_their_own_data(
        self,
        store: FabricJournalStore,
    ) -> None:
        """Wildcard scope at the caller side matches a specific entity
        scope — that's the bidirectional containment rule."""

        await store.create(_obj(name="Lead A"), scope=["org:sales:leads"])
        await store.create(_obj(name="Report B"), scope=["org:finance:reports"])

        sales = await store.query(FabricQuery(), requester_scopes=["org:sales:*"])
        assert sales.total == 1
        assert sales.objects[0].properties["name"] == "Lead A"

    @pytest.mark.asyncio
    async def test_unscoped_caller_sees_everything(
        self,
        store: FabricJournalStore,
    ) -> None:
        """``requester_scopes=None`` is the admin/system path — no
        filter is applied."""

        await store.create(_obj(name="Lead A"), scope=["org:sales:leads"])
        await store.create(_obj(name="Report B"), scope=["org:finance:reports"])

        admin = await store.query(FabricQuery(), requester_scopes=None)
        assert admin.total == 2


# ---------------------------------------------------------------------------
# 3. Pagination correctness — the bug #938 couldn't close
# ---------------------------------------------------------------------------


class TestPaginationCorrectness:
    @pytest.mark.asyncio
    async def test_total_reflects_post_filter_count(
        self,
        store: FabricJournalStore,
    ) -> None:
        """Create 10 objects split 5/5 between two scopes. A caller
        scoped to one should see total=5 regardless of page size —
        never the pre-filter 10."""

        for i in range(5):
            await store.create(_obj(name=f"Sales {i}"), scope=["org:sales:leads"])
        for i in range(5):
            await store.create(
                _obj(name=f"Finance {i}"),
                scope=["org:finance:reports"],
            )

        # Page 1 of 3 from the filtered view.
        page1 = await store.query(
            FabricQuery(limit=3, offset=0),
            requester_scopes=["org:sales:*"],
        )
        assert page1.total == 5  # NEVER 10.
        assert len(page1.objects) == 3

        # Page 2 of 3 lands the remainder.
        page2 = await store.query(
            FabricQuery(limit=3, offset=3),
            requester_scopes=["org:sales:*"],
        )
        assert page2.total == 5
        assert len(page2.objects) == 2

    @pytest.mark.asyncio
    async def test_pagination_never_leaks_hidden_objects(
        self,
        store: FabricJournalStore,
    ) -> None:
        """Even with a very large limit, the filtered view should never
        return anything from a scope the caller doesn't have access to."""

        for i in range(20):
            await store.create(_obj(name=f"Hidden {i}"), scope=["org:finance:reports"])
        await store.create(_obj(name="Visible"), scope=["org:sales:leads"])

        result = await store.query(
            FabricQuery(limit=1000),
            requester_scopes=["org:sales:*"],
        )
        assert result.total == 1
        assert result.objects[0].properties["name"] == "Visible"


# ---------------------------------------------------------------------------
# 4. Projection rebuild from journal
# ---------------------------------------------------------------------------


class TestProjectionRebuild:
    @pytest.mark.asyncio
    async def test_rebuild_from_genesis_matches_live_state(
        self,
        journal,
    ) -> None:
        """Write a sequence of events, then drop the projection and
        rebuild it. The new projection should see the same current
        state as the live one did."""

        live = FabricJournalStore(journal)
        live.bootstrap()

        a = _obj(name="A")
        b = _obj(name="B")
        c = _obj(name="C")
        await live.create(a, scope=["org:sales:leads"])
        await live.create(b, scope=["org:sales:leads"])
        await live.create(c, scope=["org:sales:leads"])
        await live.update(a.id, {"revenue": 100}, scope=["org:sales:leads"])
        await live.archive(c.id, scope=["org:sales:leads"])

        live_result = await live.query(
            FabricQuery(limit=100),
            requester_scopes=None,
        )
        live_ids = {o.id: o.properties for o in live_result.objects}

        # Drop and rebuild from genesis.
        cold = FabricJournalStore(journal, projection=FabricProjection())
        applied = cold.bootstrap()
        assert applied >= 5  # create*3 + update + archive

        cold_result = await cold.query(
            FabricQuery(limit=100),
            requester_scopes=None,
        )
        cold_ids = {o.id: o.properties for o in cold_result.objects}

        assert cold_ids == live_ids
        assert a.id in cold_ids
        assert cold_ids[a.id]["revenue"] == 100
        assert c.id not in cold_ids  # archived


# ---------------------------------------------------------------------------
# 5. Incremental apply after rebuild
# ---------------------------------------------------------------------------


class TestIncrementalApply:
    @pytest.mark.asyncio
    async def test_single_event_delta_after_rebuild(self, journal) -> None:
        """After a rebuild, appending one new event and applying it
        should change exactly one object's state — not trigger a full
        re-replay."""

        warm = FabricJournalStore(journal)
        warm.bootstrap()
        a = _obj(name="A")
        await warm.create(a, scope=["org:sales:leads"])

        cold = FabricJournalStore(journal, projection=FabricProjection())
        cold.bootstrap()
        before = cold.projection.size()
        assert before == 1

        b = _obj(name="B")
        await cold.create(b, scope=["org:sales:leads"])
        after = cold.projection.size()
        assert after == 2

        result = await cold.query(FabricQuery(limit=100), requester_scopes=None)
        names = sorted(o.properties["name"] for o in result.objects)
        assert names == ["A", "B"]


# ---------------------------------------------------------------------------
# 6. Scope-required-on-write invariant
# ---------------------------------------------------------------------------


class TestScopeRequiredOnWrite:
    @pytest.mark.asyncio
    async def test_create_rejects_empty_scope(self, store: FabricJournalStore) -> None:
        """The journal's EventEntry invariant demands non-empty scope.
        The store raises early with a Fabric-flavoured error so the
        caller sees the problem at the API boundary, not deep inside
        a pydantic validator."""

        with pytest.raises(ValueError, match="non-empty scope"):
            await store.create(_obj(name="X"), scope=[])

    @pytest.mark.asyncio
    async def test_update_rejects_empty_scope(self, store: FabricJournalStore) -> None:
        obj = _obj(name="X")
        await store.create(obj, scope=["org:sales:leads"])
        with pytest.raises(ValueError, match="non-empty scope"):
            await store.update(obj.id, {"revenue": 1}, scope=[])

    @pytest.mark.asyncio
    async def test_archive_rejects_empty_scope(self, store: FabricJournalStore) -> None:
        obj = _obj(name="X")
        await store.create(obj, scope=["org:sales:leads"])
        with pytest.raises(ValueError, match="non-empty scope"):
            await store.archive(obj.id, scope=[])


# ---------------------------------------------------------------------------
# 7. Actor attribution — the journal records who wrote each event
# ---------------------------------------------------------------------------


class TestActorAttribution:
    @pytest.mark.asyncio
    async def test_create_records_custom_actor(
        self,
        store: FabricJournalStore,
        journal,
    ) -> None:
        """When the caller supplies an Actor, the journal records it
        verbatim. Tests can pull the event back out via Journal.query()
        to confirm."""

        actor = Actor(kind="user", id="user:alice", scope_context=["org:sales:*"])
        await store.create(
            _obj(name="Acme"),
            scope=["org:sales:leads"],
            actor=actor,
        )

        events = journal.query(action=ACTION_OBJECT_CREATED)
        assert len(events) == 1
        assert events[0].actor.id == "user:alice"
        assert events[0].actor.kind == "user"

    @pytest.mark.asyncio
    async def test_default_actor_is_system_fabric(
        self,
        store: FabricJournalStore,
        journal,
    ) -> None:
        """Omitting the actor falls back to the built-in system actor —
        the one operators expect when no caller identity is available."""

        await store.create(_obj(name="Acme"), scope=["org:sales:leads"])

        events = journal.query(action=ACTION_OBJECT_CREATED)
        assert events[0].actor.kind == "system"
        assert events[0].actor.id == "system:fabric"


# ---------------------------------------------------------------------------
# 8. Policy engine — verbatim port from #938, keep its invariants.
# ---------------------------------------------------------------------------


class TestPolicyVerbatim:
    """These mirror the #938 policy tests. Ported here so the decision
    logic is guaranteed to have coverage in its new home — we don't want
    a future refactor to quietly delete the tests that came with the
    only worthwhile slice of the old PR."""

    def test_visible_unscoped_caller_sees_everything(self) -> None:
        assert visible(_obj(), None) is True
        # _obj has no scope attribute yet — attach one.
        e = _obj(name="x")
        object.__setattr__(e, "scope", ["org:finance:*"])
        assert visible(e, []) is True

    def test_visible_exact_match(self) -> None:
        e = _obj(name="x")
        object.__setattr__(e, "scope", ["org:sales:leads"])
        assert visible(e, ["org:sales:leads"]) is True

    def test_visible_glob_match(self) -> None:
        e = _obj(name="x")
        object.__setattr__(e, "scope", ["org:sales:leads"])
        assert visible(e, ["org:sales:*"]) is True

    def test_visible_no_overlap_denied(self) -> None:
        e = _obj(name="x")
        object.__setattr__(e, "scope", ["org:finance:*"])
        assert visible(e, ["org:sales:*"]) is False

    def test_filter_visible_counts_hidden(self) -> None:
        a = _obj(name="a")
        object.__setattr__(a, "scope", ["org:sales:leads"])
        b = _obj(name="b")
        object.__setattr__(b, "scope", ["org:finance:reports"])
        kept, hidden = filter_visible([a, b], ["org:sales:*"])
        assert len(kept) == 1
        assert hidden == 1

    def test_decide_records_matched_scope(self) -> None:
        e = _obj(name="x")
        object.__setattr__(e, "scope", ["org:sales:leads"])
        d = decide(e, ["org:other:*", "org:sales:*"])
        assert isinstance(d, PolicyDecision)
        assert d.allowed is True
        assert d.matched_scope == "org:sales:*"
