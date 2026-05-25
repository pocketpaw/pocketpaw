# tests/ee/test_decision_graph_api.py — RFC 07 Slice 1 in-process API
# tests. Covers the four DecisionGraph methods (get, find, trace,
# downstream) including scope-filter enforcement on every method.
#
# Created: 2026-05-25 (feat/rfc07-decision-graph-slice1).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.service import DecisionGraph
from pocketpaw_ee.cloud.decisions.store import DecisionStore, set_db_path
from soul_protocol.spec.journal import Actor, EventEntry


@pytest.fixture
def store(tmp_path: Path) -> DecisionStore:
    set_db_path(tmp_path / "decisions.db")
    s = DecisionStore()
    yield s
    s.close()


@pytest.fixture
def projection(store: DecisionStore) -> DecisionProjection:
    return DecisionProjection(store=store)


@pytest.fixture
def graph(store: DecisionStore, projection: DecisionProjection) -> DecisionGraph:
    return DecisionGraph(store=store, projection=projection)


@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers — duplicated tiny bit from the projection tests to keep this
# file self-contained
# ---------------------------------------------------------------------------


def _event(
    *,
    seq: int,
    ts: datetime,
    actor: Actor,
    action: str,
    correlation_id: UUID | None,
    payload: dict,
    scope: list[str] | None = None,
) -> EventEntry:
    return EventEntry(
        id=uuid4(),
        ts=ts,
        actor=actor,
        action=action,
        scope=scope or ["org:nerve"],
        correlation_id=correlation_id,
        payload=payload,
        seq=seq,
    )


def _seed_chain(
    projection: DecisionProjection,
    *,
    base_ts: datetime,
    pocket_id: str = "pa",
    scope: list[str] | None = None,
    action_name: str = "send_to_tenant",
    actor_id: str = "did:soul:agent1",
    precedents: list[dict] | None = None,
    seq_base: int = 1000,
) -> UUID:
    """Seed one approval chain in the store; return the chain's
    correlation_id. Returns the FIRST `decision.completed` Decision's
    id via a helper lookup post-emit."""
    corr = uuid4()
    scope = scope or ["org:nerve", f"pocket:{pocket_id}"]
    actor = Actor(kind="agent", id=actor_id, scope_context=scope)
    payload = {
        "intent": f"chain-{seq_base}",
        "action": action_name,
        "pocket_id": pocket_id,
    }
    if precedents is not None:
        payload["precedents"] = precedents
    events = [
        _event(
            seq=seq_base,
            ts=base_ts,
            actor=actor,
            action="agent.proposed",
            correlation_id=corr,
            payload=payload,
            scope=scope,
        ),
        _event(
            seq=seq_base + 1,
            ts=base_ts + timedelta(seconds=1),
            actor=actor,
            action="decision.completed",
            correlation_id=corr,
            payload={"passed": True},
            scope=scope,
        ),
    ]
    last_decision_id: UUID | None = None
    for e in events:
        result = projection.apply(e)
        if result is not None:
            last_decision_id = result.id
    assert last_decision_id is not None
    return last_decision_id


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_decision(graph, projection, base_ts) -> None:
    decision_id = _seed_chain(projection, base_ts=base_ts)
    found = await graph.get(decision_id)
    assert found is not None
    assert found.id == decision_id


@pytest.mark.asyncio
async def test_get_returns_none_when_missing(graph) -> None:
    found = await graph.get(uuid4())
    assert found is None


@pytest.mark.asyncio
async def test_get_returns_none_when_scope_mismatch(graph, projection, base_ts) -> None:
    """Scope filter on get() — outside-scope caller gets None (not a probe)."""
    decision_id = _seed_chain(projection, base_ts=base_ts, scope=["org:nerve", "workspace:a"])
    found = await graph.get(decision_id, requester_scopes=["workspace:b"])
    assert found is None


# ---------------------------------------------------------------------------
# find()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_filters_by_actor(graph, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts, actor_id="did:soul:agent_a", seq_base=2000)
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=10),
        actor_id="did:soul:agent_b",
        seq_base=2100,
    )
    a_only = await graph.find(actor="did:soul:agent_a")
    assert len(a_only) == 1
    assert a_only[0].decided_by.id == "did:soul:agent_a"


@pytest.mark.asyncio
async def test_find_filters_by_pocket(graph, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts, pocket_id="pocket_alpha", seq_base=2200)
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=10),
        pocket_id="pocket_beta",
        seq_base=2300,
    )
    alpha = await graph.find(pocket_id="pocket_alpha")
    assert len(alpha) == 1
    assert alpha[0].pocket_id == "pocket_alpha"


@pytest.mark.asyncio
async def test_find_filters_by_time_window(graph, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts, seq_base=2400)
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(hours=2),
        seq_base=2500,
    )
    only_early = await graph.find(until=base_ts + timedelta(hours=1))
    assert len(only_early) == 1


@pytest.mark.asyncio
async def test_find_keyset_pagination(graph, projection, base_ts) -> None:
    """Keyset pagination on (ts DESC, id DESC) — page 2 uses page 1's
    last (ts, id) as the cursor."""
    for i in range(5):
        _seed_chain(
            projection,
            base_ts=base_ts + timedelta(seconds=i),
            seq_base=2600 + i * 10,
        )
    page_one = await graph.find(limit=2)
    assert len(page_one) == 2
    last = page_one[-1]
    page_two = await graph.find(limit=2, before_ts=last.ts, before_id=str(last.id))
    assert len(page_two) == 2
    # No overlap.
    ids_one = {d.id for d in page_one}
    ids_two = {d.id for d in page_two}
    assert ids_one.isdisjoint(ids_two)


@pytest.mark.asyncio
async def test_find_scope_filter_per_call(graph, projection, base_ts) -> None:
    """Scope filter on find() — A-scope caller sees only A-scope rows."""
    _seed_chain(projection, base_ts=base_ts, scope=["org:nerve", "workspace:a"], seq_base=2700)
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=10),
        scope=["org:nerve", "workspace:b"],
        seq_base=2800,
    )
    a_scoped = await graph.find(requester_scopes=["workspace:a"])
    assert len(a_scoped) == 1
    assert "workspace:a" in a_scoped[0].scope


# ---------------------------------------------------------------------------
# trace() — upstream walk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_walks_precedents(graph, projection, base_ts) -> None:
    """A decision with an agent-supplied precedent has an edge to the
    precedent that the trace walks."""
    # Seed two prior decisions; capture their ids.
    seed_ids: list[UUID] = []
    for i in range(2):
        sid = _seed_chain(
            projection,
            base_ts=base_ts - timedelta(days=2 - i),
            seq_base=3000 + i * 10,
        )
        seed_ids.append(sid)

    # Now a new decision with explicit precedents pointing at the seeds.
    new_id = _seed_chain(
        projection,
        base_ts=base_ts,
        precedents=[
            {"decision_id": str(seed_ids[0]), "weight": 0.9},
            {"decision_id": str(seed_ids[1]), "weight": 0.7},
        ],
        seq_base=3100,
    )
    trace = await graph.trace(new_id, depth=2)
    # The two precedent nodes are walked.
    node_ids = set(trace.nodes.keys())
    assert str(new_id) in node_ids
    assert str(seed_ids[0]) in node_ids
    assert str(seed_ids[1]) in node_ids
    # At least the precedent edges are present.
    precedent_edges = [e for e in trace.edges if e.relation == "precedent"]
    assert len(precedent_edges) >= 2


@pytest.mark.asyncio
async def test_trace_respects_scope_filter(graph, projection, base_ts) -> None:
    """A precedent in a different scope does NOT appear in the trace."""
    # Seed precedent in scope B.
    b_id = _seed_chain(
        projection,
        base_ts=base_ts - timedelta(days=1),
        scope=["org:nerve", "workspace:b"],
        seq_base=3200,
    )
    # A-scoped Decision points at the B-scoped precedent (synthetic; the
    # projection doesn't normally cross-scope-link, but the edge could
    # exist if a payload references it).
    new_id = _seed_chain(
        projection,
        base_ts=base_ts,
        scope=["org:nerve", "workspace:a"],
        precedents=[{"decision_id": str(b_id), "weight": 0.9}],
        seq_base=3300,
    )
    # Trace with A-scope only.
    trace = await graph.trace(new_id, depth=2, requester_scopes=["workspace:a"])
    # The B-scoped precedent must NOT appear as a node.
    assert str(b_id) not in trace.nodes


@pytest.mark.asyncio
async def test_trace_returns_empty_for_missing_root(graph) -> None:
    trace = await graph.trace(uuid4(), depth=2)
    assert trace.nodes == {}
    assert trace.edges == []


# ---------------------------------------------------------------------------
# downstream() — inverse precedent walk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_downstream_finds_later_citers(graph, projection, base_ts) -> None:
    """Decisions that later cite this one show up in downstream."""
    # The root (older) decision.
    root_id = _seed_chain(projection, base_ts=base_ts - timedelta(days=1), seq_base=4000)

    # Two later decisions that cite it.
    citer_one = _seed_chain(
        projection,
        base_ts=base_ts,
        precedents=[{"decision_id": str(root_id), "weight": 0.8}],
        seq_base=4100,
    )
    citer_two = _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=5),
        precedents=[{"decision_id": str(root_id), "weight": 0.6}],
        seq_base=4200,
    )

    down = await graph.downstream(root_id, depth=2)
    node_ids = set(down.nodes.keys())
    assert str(root_id) in node_ids
    assert str(citer_one) in node_ids
    assert str(citer_two) in node_ids
    # Inverse edges are stamped as "downstream" in the result.
    downstream_edges = [e for e in down.edges if e.relation == "downstream"]
    assert len(downstream_edges) == 2


@pytest.mark.asyncio
async def test_downstream_respects_scope_filter(graph, projection, base_ts) -> None:
    """A citer in a different scope does NOT appear in downstream."""
    root_id = _seed_chain(
        projection,
        base_ts=base_ts - timedelta(days=1),
        scope=["org:nerve", "workspace:a"],
        seq_base=4300,
    )
    # B-scoped citer.
    _seed_chain(
        projection,
        base_ts=base_ts,
        scope=["org:nerve", "workspace:b"],
        precedents=[{"decision_id": str(root_id), "weight": 0.9}],
        seq_base=4400,
    )
    # A-scope only — the B citer must not appear.
    down = await graph.downstream(root_id, depth=2, requester_scopes=["workspace:a"])
    # Only root should be visible.
    assert len(down.nodes) == 1
    assert str(root_id) in down.nodes
