# tests/ee/test_decisions_router.py — RFC 07 Slice 2 router coverage.
# Created: 2026-05-25 — pins the five real REST routes shipped in
#   `pocketpaw_ee.cloud.decisions.router` (the Slice 1 ping route stays;
#   the rest replaces the stub):
# Updated: 2026-05-25 (RFC 07 Slice 2 — post-filter total) — added two
#   regression tests for the list endpoint: `test_total_is_post_scope_
#   filter_not_page_size` (the load-bearing anti-probe assertion) and
#   `test_partial_page_returns_null_cursor` (a short page must NOT echo
#   a phantom `next_before_*` cursor).
#
#     GET  /api/v1/decisions/_ping       — Slice 1 liveness; smoke test
#     GET  /api/v1/decisions/:id          — single lookup, 404 + scope hide
#     GET  /api/v1/decisions              — list w/ filters, keyset paging
#     GET  /api/v1/decisions/:id/trace    — upstream BFS, depth + truncation
#     GET  /api/v1/decisions/:id/downstream — inverse precedent walk
#     GET  /api/v1/decisions/:id/timeline — flattened journal events
#
#   Tests mount the router on a bare FastAPI app and override the auth
#   chain (request_context) + license gate so the contract is exercised
#   without a real Mongo. Each test gets its own SQLite store via
#   `tmp_path` + `set_db_path` so projection state can't leak across
#   tests. The journal is overridden to a tmp file as well.
#
#   Tenant-isolation check: workspace A's decisions are invisible to
#   workspace B — pins the load-bearing scope filter the RFC's audit
#   contract requires.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.router import router as decisions_router
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import DecisionStore, set_db_path
from pocketpaw_ee.cloud.license import require_license
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor, EventEntry

from pocketpaw.journal_dep import get_journal, reset_journal_cache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_projection_and_journal() -> None:
    """Clear module-level singletons between tests so the projection
    + journal don't bleed state across cases.
    """
    reset_projection_for_tests()
    reset_journal_cache()
    yield
    reset_projection_for_tests()
    reset_journal_cache()


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    """A disposable SQLite journal file for the timeline test path."""
    return tmp_path / "journal.db"


@pytest.fixture
def store(tmp_path: Path) -> DecisionStore:
    """Fresh SQLite-backed decision store per test.

    The `service.get_decision_graph()` singleton resolves through
    `set_db_path`, so this fixture is the seam tests use to install a
    disposable store.
    """
    set_db_path(tmp_path / "decisions.db")
    s = DecisionStore()
    yield s
    s.close()


@pytest.fixture
def projection(store: DecisionStore) -> DecisionProjection:
    return DecisionProjection(store=store)


@pytest.fixture
def graph(store: DecisionStore, projection: DecisionProjection) -> DecisionGraph:
    """Install a DecisionGraph singleton that wraps the per-test store."""
    from pocketpaw_ee.cloud.decisions import service as decisions_service

    g = DecisionGraph(store=store, projection=projection)
    decisions_service._GRAPH = g
    return g


@pytest.fixture
def workspace_id() -> str:
    return "ws_a_test"


@pytest.fixture
def workspace_b_id() -> str:
    return "ws_b_test"


def _make_request_context(workspace_id: str | None) -> RequestContext:
    return RequestContext(
        user_id="user_test",
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def app(graph: DecisionGraph, journal_path: Path, workspace_id: str) -> FastAPI:
    """FastAPI app with the decisions router + auth + license overridden.

    The default ctx pins the workspace to `workspace_id` (workspace A);
    individual tests that want workspace B reach into
    `app.dependency_overrides[request_context]` and swap.
    """
    a = FastAPI()
    add_error_handler(a)  # CloudError → JSON envelope
    a.include_router(decisions_router, prefix="/api/v1")
    a.dependency_overrides[get_journal] = lambda: open_journal(journal_path)
    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[request_context] = lambda: _make_request_context(workspace_id)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Event-building helpers — modeled on tests/ee/test_decision_projection.py
# ---------------------------------------------------------------------------


def _event(
    *,
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
        scope=scope or ["org:nerve", "workspace:ws_a_test"],
        correlation_id=correlation_id,
        payload=payload,
    )


def _seed_chain(
    projection: DecisionProjection,
    *,
    base_ts: datetime,
    pocket_id: str = "p_main",
    workspace: str = "ws_a_test",
    actor_id: str = "did:soul:agent1",
    action_name: str = "send_to_tenant",
    precedents: list[dict] | None = None,
    corr: UUID | None = None,
) -> tuple[UUID, UUID]:
    """Seed one approval chain in the store and return (correlation_id,
    decision_id) so tests can address both axes."""
    corr = corr or uuid4()
    scope = ["org:nerve", f"workspace:{workspace}", f"pocket:{pocket_id}"]
    actor = Actor(kind="agent", id=actor_id, scope_context=scope)
    payload: dict = {
        "intent": f"chain-{corr.hex[:8]}",
        "action": action_name,
        "pocket_id": pocket_id,
    }
    if precedents is not None:
        payload["precedents"] = precedents
    events = [
        _event(
            ts=base_ts,
            actor=actor,
            action="agent.proposed",
            correlation_id=corr,
            payload=payload,
            scope=scope,
        ),
        _event(
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
    return corr, last_decision_id


# ---------------------------------------------------------------------------
# _ping — Slice 1 holdover; sanity test it didn't regress
# ---------------------------------------------------------------------------


def test_ping_returns_cursor_and_count(client: TestClient, graph: DecisionGraph) -> None:
    resp = client.get("/api/v1/decisions/_ping")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["decisions"] == 0  # nothing seeded
    assert "cursor" in body


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id
# ---------------------------------------------------------------------------


def test_get_returns_decision(client, projection, base_ts) -> None:
    _, decision_id = _seed_chain(projection, base_ts=base_ts)
    resp = client.get(f"/api/v1/decisions/{decision_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(decision_id)
    assert body["action"] == "send_to_tenant"
    assert body["pocket_id"] == "p_main"
    # Wire shape — flattened actor fields, no nesting.
    assert body["actor_kind"] == "agent"
    assert body["actor_id"] == "did:soul:agent1"


def test_get_returns_404_when_missing(client) -> None:
    resp = client.get(f"/api/v1/decisions/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "decisions.not_found"


def test_get_returns_400_when_id_is_not_uuid(client) -> None:
    resp = client.get("/api/v1/decisions/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "decisions.invalid_id"


def test_get_returns_404_when_scope_mismatch(
    client, app, projection, base_ts, workspace_b_id
) -> None:
    """A workspace B caller cannot see workspace A's decisions.

    The route returns the same 404 envelope as a real miss — the two
    states are deliberately indistinguishable so a caller cannot probe
    for hidden rows (RFC 07 § Privacy + audit).
    """
    _, decision_id = _seed_chain(projection, base_ts=base_ts, workspace="ws_a_test")

    # Flip the ctx to workspace B.
    app.dependency_overrides[request_context] = lambda: _make_request_context(workspace_b_id)
    resp = client.get(f"/api/v1/decisions/{decision_id}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "decisions.not_found"


# ---------------------------------------------------------------------------
# GET /api/v1/decisions — list + filters
# ---------------------------------------------------------------------------


def test_list_returns_decisions(client, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts, actor_id="did:soul:a")
    _seed_chain(projection, base_ts=base_ts + timedelta(seconds=10), actor_id="did:soul:b")
    resp = client.get("/api/v1/decisions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert len(body["decisions"]) == 2


def test_list_filters_by_actor(client, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts, actor_id="did:soul:alpha")
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=10),
        actor_id="did:soul:beta",
    )
    resp = client.get("/api/v1/decisions", params={"actor": "did:soul:alpha"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["decisions"][0]["actor_id"] == "did:soul:alpha"


def test_list_filters_by_pocket(client, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts, pocket_id="pocket_alpha")
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=10),
        pocket_id="pocket_beta",
    )
    resp = client.get("/api/v1/decisions", params={"pocket_id": "pocket_alpha"})
    body = resp.json()
    assert body["total"] == 1
    assert body["decisions"][0]["pocket_id"] == "pocket_alpha"


def test_list_filters_by_time_window(client, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts)
    _seed_chain(projection, base_ts=base_ts + timedelta(hours=2))
    resp = client.get(
        "/api/v1/decisions",
        params={"until": (base_ts + timedelta(hours=1)).isoformat()},
    )
    body = resp.json()
    assert body["total"] == 1


def test_list_keyset_pagination(client, projection, base_ts) -> None:
    """Two-page walk returns disjoint id sets — keyset cursor is correct."""
    for i in range(5):
        _seed_chain(projection, base_ts=base_ts + timedelta(seconds=i))

    page_one = client.get("/api/v1/decisions", params={"limit": 2}).json()
    assert len(page_one["decisions"]) == 2
    assert page_one["next_before_ts"] is not None
    assert page_one["next_before_id"] is not None

    page_two = client.get(
        "/api/v1/decisions",
        params={
            "limit": 2,
            "before_ts": page_one["next_before_ts"],
            "before_id": page_one["next_before_id"],
        },
    ).json()
    assert len(page_two["decisions"]) == 2
    ids_one = {d["id"] for d in page_one["decisions"]}
    ids_two = {d["id"] for d in page_two["decisions"]}
    assert ids_one.isdisjoint(ids_two)


def test_list_rejects_workspace_id_query(client) -> None:
    """The route refuses to take a `workspace_id` query — tenancy is auth-derived."""
    resp = client.get("/api/v1/decisions", params={"workspace_id": "other_workspace"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "decisions.workspace_id_forbidden"


def test_list_scope_filter_per_call(client, app, projection, base_ts, workspace_b_id) -> None:
    """Two-workspace tenant isolation — A's decisions invisible to B."""
    _seed_chain(projection, base_ts=base_ts, workspace="ws_a_test")
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=10),
        workspace="ws_b_test",
    )

    # Workspace A sees only ws_a_test's decisions.
    a_body = client.get("/api/v1/decisions").json()
    assert a_body["total"] == 1
    assert "workspace:ws_a_test" in a_body["decisions"][0]["scope"]

    # Workspace B sees only ws_b_test's decisions.
    app.dependency_overrides[request_context] = lambda: _make_request_context(workspace_b_id)
    b_body = client.get("/api/v1/decisions").json()
    assert b_body["total"] == 1
    assert "workspace:ws_b_test" in b_body["decisions"][0]["scope"]


def test_total_is_post_scope_filter_not_page_size(
    client, app, projection, base_ts, workspace_b_id
) -> None:
    """``total`` reports the post-scope-filter count, not the page size.

    Load-bearing test for the RFC 07 § Privacy anti-probe property: a
    caller varying ``limit`` MUST NOT observe a changing ``total``. If
    ``total`` echoed the page size, a caller could compare
    ``total(limit=N)`` to a workspace-wide count to infer hidden rows
    in their own scope; if ``total`` echoed the pre-scope-filter count,
    the leak would be even worse — a caller would see how many rows
    sit in OTHER workspaces.
    """
    # 5 in workspace A, 3 in workspace B. The route's caller (this
    # client) is workspace A by default.
    for i in range(5):
        _seed_chain(
            projection,
            base_ts=base_ts + timedelta(seconds=i),
            workspace="ws_a_test",
        )
    for i in range(3):
        _seed_chain(
            projection,
            base_ts=base_ts + timedelta(seconds=100 + i),
            workspace="ws_b_test",
        )

    # Page-sized request: limit=2 (well under the 5 we seeded for A).
    body = client.get("/api/v1/decisions", params={"limit": 2}).json()

    # The page is the requested 2 rows.
    assert len(body["decisions"]) == 2
    # `total` is workspace A's full scoped count, NOT the page size...
    assert body["total"] == 5
    # ...and NOT the unscoped total (which would leak workspace B).
    assert body["total"] != 8
    # ...and NOT the requested page size.
    assert body["total"] != 2

    # Sanity: varying `limit` does not move `total`. This is the
    # invariant a probe would try to violate.
    body_full = client.get("/api/v1/decisions", params={"limit": 50}).json()
    assert body_full["total"] == 5

    # Workspace B sees its own 3 — same scope-aware shape.
    app.dependency_overrides[request_context] = lambda: _make_request_context(workspace_b_id)
    body_b = client.get("/api/v1/decisions", params={"limit": 1}).json()
    assert len(body_b["decisions"]) == 1
    assert body_b["total"] == 3


def test_partial_page_returns_null_cursor(client, projection, base_ts) -> None:
    """A partial page (fewer rows returned than the limit) MUST NOT echo
    a ``next_before_*`` cursor — the client would otherwise follow a
    phantom next page that always returns empty.
    """
    # Seed 3 decisions; request limit=10 so the response is partial.
    for i in range(3):
        _seed_chain(projection, base_ts=base_ts + timedelta(seconds=i))

    body = client.get("/api/v1/decisions", params={"limit": 10}).json()
    assert len(body["decisions"]) == 3
    assert body["next_before_ts"] is None
    assert body["next_before_id"] is None


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/trace
# ---------------------------------------------------------------------------


def test_trace_walks_precedents_at_default_depth(client, projection, base_ts) -> None:
    # Seed a precedent.
    _, prec_id = _seed_chain(projection, base_ts=base_ts - timedelta(days=1))
    _, new_id = _seed_chain(
        projection,
        base_ts=base_ts,
        precedents=[{"decision_id": str(prec_id), "weight": 0.9}],
    )

    resp = client.get(f"/api/v1/decisions/{new_id}/trace")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["root"] == str(new_id)
    assert str(new_id) in body["nodes"]
    assert str(prec_id) in body["nodes"]


def test_trace_depth_1(client, projection, base_ts) -> None:
    """depth=1 shows the root but doesn't walk further."""
    _, prec_id = _seed_chain(projection, base_ts=base_ts - timedelta(days=2))
    _, new_id = _seed_chain(
        projection,
        base_ts=base_ts,
        precedents=[{"decision_id": str(prec_id), "weight": 0.9}],
    )

    resp = client.get(f"/api/v1/decisions/{new_id}/trace", params={"depth": 1})
    body = resp.json()
    assert body["root"] == str(new_id)
    assert body["depth_reached"] <= 1


def test_trace_depth_10_is_max(client, projection, base_ts) -> None:
    """depth=10 is the cap (RFC 07 perf budget)."""
    _, root_id = _seed_chain(projection, base_ts=base_ts)
    resp = client.get(f"/api/v1/decisions/{root_id}/trace", params={"depth": 10})
    assert resp.status_code == 200


def test_trace_depth_above_max_rejected(client, projection, base_ts) -> None:
    """depth=11 is rejected at the validation layer."""
    _, root_id = _seed_chain(projection, base_ts=base_ts)
    resp = client.get(f"/api/v1/decisions/{root_id}/trace", params={"depth": 11})
    # FastAPI's ge/le validation returns 422 for query-param violations.
    assert resp.status_code == 422


def test_trace_truncates_high_fanout(client, projection, base_ts) -> None:
    """A node with more outgoing edges than `max_fanout` is truncated and
    the response reports it."""
    # Seed many precedents; cite all from one new decision.
    prec_ids: list[UUID] = []
    for i in range(15):
        _, p = _seed_chain(projection, base_ts=base_ts - timedelta(days=10, seconds=i))
        prec_ids.append(p)

    _, new_id = _seed_chain(
        projection,
        base_ts=base_ts,
        precedents=[{"decision_id": str(p), "weight": 1.0} for p in prec_ids],
    )

    resp = client.get(
        f"/api/v1/decisions/{new_id}/trace",
        params={"depth": 2, "max_fanout": 3},
    )
    body = resp.json()
    assert body["truncated"] is True
    assert body["truncated_count"] > 0


def test_trace_returns_404_for_missing_root(client) -> None:
    resp = client.get(f"/api/v1/decisions/{uuid4()}/trace")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/downstream
# ---------------------------------------------------------------------------


def test_downstream_finds_later_citers(client, projection, base_ts) -> None:
    _, root_id = _seed_chain(projection, base_ts=base_ts - timedelta(days=1))
    _, citer_id = _seed_chain(
        projection,
        base_ts=base_ts,
        precedents=[{"decision_id": str(root_id), "weight": 0.8}],
    )

    resp = client.get(f"/api/v1/decisions/{root_id}/downstream")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert str(root_id) in body["nodes"]
    assert str(citer_id) in body["nodes"]
    # Inverse edges are stamped as "downstream" on the wire.
    downstream_edges = [e for e in body["edges"] if e["relation"] == "downstream"]
    assert len(downstream_edges) == 1


def test_downstream_returns_404_for_missing_root(client) -> None:
    resp = client.get(f"/api/v1/decisions/{uuid4()}/downstream")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/timeline
# ---------------------------------------------------------------------------


def test_timeline_returns_journal_events(client, projection, base_ts, journal_path: Path) -> None:
    """The timeline endpoint reads the journal for events sharing the
    Decision's correlation_id and returns them in seq order."""
    # Seed a Decision via the projection — chain has a correlation_id.
    corr = uuid4()
    _, decision_id = _seed_chain(projection, base_ts=base_ts, corr=corr)

    # Append the same events to the real journal so the timeline endpoint
    # can read them back. Each entry shares the `corr` correlation_id.
    journal = open_journal(journal_path)
    actor = Actor(
        kind="agent",
        id="did:soul:agent1",
        scope_context=["org:nerve", "workspace:ws_a_test", "pocket:p_main"],
    )
    journal.append(
        EventEntry(
            id=uuid4(),
            ts=base_ts,
            actor=actor,
            action="agent.proposed",
            scope=["org:nerve", "workspace:ws_a_test", "pocket:p_main"],
            correlation_id=corr,
            payload={"intent": "test"},
        )
    )
    journal.append(
        EventEntry(
            id=uuid4(),
            ts=base_ts + timedelta(seconds=1),
            actor=actor,
            action="decision.completed",
            scope=["org:nerve", "workspace:ws_a_test", "pocket:p_main"],
            correlation_id=corr,
            payload={"passed": True},
        )
    )

    resp = client.get(f"/api/v1/decisions/{decision_id}/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_id"] == str(decision_id)
    assert body["correlation_id"] == str(corr)
    assert len(body["events"]) == 2
    # Events are seq-ordered.
    assert body["events"][0]["seq"] < body["events"][1]["seq"]
    assert body["events"][0]["action"] == "agent.proposed"
    assert body["events"][1]["action"] == "decision.completed"


def test_timeline_returns_404_when_decision_missing(client) -> None:
    resp = client.get(f"/api/v1/decisions/{uuid4()}/timeline")
    assert resp.status_code == 404


def test_timeline_returns_404_when_scope_mismatch(
    client, app, projection, base_ts, workspace_b_id
) -> None:
    """Timeline is gated by the same scope check as the get endpoint —
    workspace B caller cannot read workspace A's events."""
    _, decision_id = _seed_chain(projection, base_ts=base_ts, workspace="ws_a_test")

    app.dependency_overrides[request_context] = lambda: _make_request_context(workspace_b_id)
    resp = client.get(f"/api/v1/decisions/{decision_id}/timeline")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "decisions.not_found"
