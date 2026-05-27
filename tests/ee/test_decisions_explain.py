# tests/ee/test_decisions_explain.py — RFC 07 Slice 3a explain coverage.
# Created: 2026-05-25 — pins the natural-language explain pipeline shipped
#   in `pocketpaw_ee.cloud.decisions.explain` and the
#   `POST /api/v1/decisions/explain` REST route. Test surface:
#
#     - Templated narrator: walks a synthetic chain, asserts every
#       Decision gets a short-id citation in the narrative.
#     - LLM narrator path: mocks the Anthropic client and asserts the
#       grounding verifier strips ungrounded sentences.
#     - Cache hit / miss: first call populates, second identical call
#       returns the cached row without re-narrating.
#     - Cache invalidation: a new event folded into the projection drops
#       any cache entry whose `decisions_walked` includes the affected
#       decision.
#     - Scope filtering: a workspace-B caller never sees workspace-A
#       decisions through the explain route.
#     - End-to-end: route + extractor + find + trace + narrator + cache
#       + response, with the LLM call mocked so the test stays hermetic.
"""Tests for the natural-language explain pipeline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.decisions.dto import ExplanationResponse
from pocketpaw_ee.cloud.decisions.explain import (
    ExplainRequestInput,
    explain,
    narrate_decision,
)
from pocketpaw_ee.cloud.decisions.explain.cache import (
    build_cache_key,
    get_explain_cache,
    reset_explain_cache_for_tests,
)
from pocketpaw_ee.cloud.decisions.explain.extractor import (
    ExtractedEntities,
    extract_entities,
)
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.router import router as decisions_router
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import DecisionStore, set_db_path
from pocketpaw_ee.cloud.license import require_license
from soul_protocol.spec.journal import Actor, EventEntry

# ---------------------------------------------------------------------------
# Fixtures — fresh projection + store + cache per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_state() -> None:
    reset_projection_for_tests()
    reset_explain_cache_for_tests()
    yield
    reset_projection_for_tests()
    reset_explain_cache_for_tests()


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
    from pocketpaw_ee.cloud.decisions import service as decisions_service

    g = DecisionGraph(store=store, projection=projection)
    decisions_service._GRAPH = g
    return g


@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def workspace_id() -> str:
    return "ws_a_test"


@pytest.fixture
def app(graph: DecisionGraph, workspace_id: str) -> FastAPI:
    a = FastAPI()
    add_error_handler(a)
    a.include_router(decisions_router, prefix="/api/v1")
    a.dependency_overrides[require_license] = lambda: None
    a.dependency_overrides[request_context] = lambda: RequestContext(
        user_id="user_test",
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers — chain seeding, mirrors test_decisions_router._seed_chain
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
    inputs: list[dict] | None = None,
    intent: str = "Approve the tenant renewal",
    approver_user: str | None = None,
    instinct_policy: str | None = None,
    instinct_passed: bool | None = None,
) -> UUID:
    """Seed one approval chain and return the emitted decision id."""
    corr = uuid4()
    scope = ["org:nerve", f"workspace:{workspace}", f"pocket:{pocket_id}"]
    actor = Actor(kind="agent", id=actor_id, scope_context=scope)
    payload: dict = {
        "intent": intent,
        "action": action_name,
        "pocket_id": pocket_id,
        "inputs": inputs
        or [{"kind": "fabric_object", "id": "lease:LR-2026-117", "label": "Lease LR-2026-117"}],
    }
    if precedents is not None:
        payload["precedents"] = precedents
    events: list[EventEntry] = [
        _event(
            ts=base_ts,
            actor=actor,
            action="agent.proposed",
            correlation_id=corr,
            payload=payload,
            scope=scope,
        ),
    ]
    if instinct_policy is not None:
        events.append(
            _event(
                ts=base_ts + timedelta(milliseconds=100),
                actor=Actor(kind="system", id="system:instinct", scope_context=scope),
                action="policy.evaluated",
                correlation_id=corr,
                payload={"policy": instinct_policy, "passed": bool(instinct_passed)},
                scope=scope,
            )
        )
    if approver_user:
        events.append(
            _event(
                ts=base_ts + timedelta(seconds=1),
                actor=Actor(kind="user", id=approver_user, scope_context=scope),
                action="human.corrected",
                correlation_id=corr,
                payload={"action": "approve"},
                scope=scope,
            )
        )
    events.append(
        _event(
            ts=base_ts + timedelta(seconds=2),
            actor=actor,
            action="decision.graduated",
            correlation_id=corr,
            payload={"passed": True},
            scope=scope,
        )
    )
    last_id: UUID | None = None
    for e in events:
        result = projection.apply(e)
        if result is not None:
            last_id = result.id
    assert last_id is not None
    return last_id


# ---------------------------------------------------------------------------
# Extractor — templated path (always available)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractor_templated_recovers_fabric_object_and_date() -> None:
    """The templated path recognises a bare LR-id + a "April 22" date."""
    out = await extract_entities(
        "Why was lease renewal LR-2026-117 approved on April 22?",
        backend="templated",
    )
    assert "lease:LR-2026-117" in out.fabric_object_refs
    assert out.outcome_hint == "approved"
    assert out.time_range is not None
    start, end = out.time_range
    assert start is not None and start.month == 4 and start.day == 22


@pytest.mark.asyncio
async def test_extractor_templated_recovers_actor_and_pocket() -> None:
    out = await extract_entities(
        "Show me everything Prakash approved in pocket p_renewals",
        backend="templated",
    )
    assert "user:prakash" in out.actor_refs
    assert out.pocket_id == "p_renewals"
    assert out.outcome_hint == "approved"


@pytest.mark.asyncio
async def test_extractor_falls_back_to_templated_when_no_api_key() -> None:
    """No API key => templated path runs without an LLM call."""
    out = await extract_entities(
        "Why was LR-2026-117 approved?",
        api_key=None,
    )
    assert isinstance(out, ExtractedEntities)
    assert "lease:LR-2026-117" in out.fabric_object_refs


# ---------------------------------------------------------------------------
# Templated narrator — walks a 4-decision chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_templated_narrator_cites_every_decision(projection, base_ts) -> None:
    """Walk a chain of 4 precedents → root decision and assert every
    Decision id (short form) is cited at least once in the narrative.

    Templated narrator currently emits root-id citations on every
    sentence; precedent ids appear in the precedents sentence. The
    test asserts both surfaces work — the root id is everywhere; each
    precedent id appears in its own clause.
    """
    # 4 precedent decisions (older than root)
    prec_ids: list[UUID] = []
    for i in range(4):
        pid = _seed_chain(
            projection,
            base_ts=base_ts - timedelta(days=4 - i),
            intent=f"precedent chain {i}",
            action_name=f"prior_action_{i}",
        )
        prec_ids.append(pid)

    root_id = _seed_chain(
        projection,
        base_ts=base_ts,
        precedents=[{"decision_id": str(p), "weight": 0.9} for p in prec_ids[:3]],
    )

    # Use the cloud graph via the singleton (graph fixture already
    # set it). Pull the trace + narrate.
    from pocketpaw_ee.cloud.decisions.service import get_decision_graph

    graph = get_decision_graph()
    root = await graph.get(root_id)
    assert root is not None
    trace = await graph.trace(root_id, depth=2)

    explanation = await narrate_decision(root, trace, backend="templated")

    # Root id is cited.
    assert str(root_id)[:8] in explanation.narrative
    # Every precedent id makes it into either the narrative or the walked list.
    for p in prec_ids[:3]:
        short = str(p)[:8]
        assert short in explanation.narrative, (
            f"precedent {short} missing from narrative: {explanation.narrative}"
        )
    # decisions_walked contains every node we touched.
    assert root_id in explanation.decisions_walked
    for p in prec_ids[:3]:
        assert p in explanation.decisions_walked
    assert explanation.backend_used == "templated"


# ---------------------------------------------------------------------------
# LLM narrator path — mock the Anthropic client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_narrator_strips_ungrounded_sentences(projection, base_ts) -> None:
    """The mocked LLM returns a narrative with one cited + one ungrounded
    sentence. The verifier keeps the cited one, strips the second."""
    root_id = _seed_chain(projection, base_ts=base_ts)

    from pocketpaw_ee.cloud.decisions.service import get_decision_graph

    graph = get_decision_graph()
    root = await graph.get(root_id)
    assert root is not None
    trace = await graph.trace(root_id, depth=2)

    short = str(root_id)[:8]
    fake_text = (
        f"The decision was approved with intent to renew the lease [{short}]. "
        f"The team had a long debate over rent strategy before settling."  # no citation
    )

    fake_content = MagicMock()
    fake_content.text = fake_text
    fake_response = MagicMock()
    fake_response.content = [fake_content]
    fake_response.usage = MagicMock(input_tokens=120, output_tokens=40)

    fake_messages = MagicMock()
    fake_messages.create = AsyncMock(return_value=fake_response)
    fake_client = MagicMock()
    fake_client.messages = fake_messages

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        explanation = await narrate_decision(root, trace, backend="llm", api_key="sk-fake")

    # The grounded sentence is kept; the ungrounded one stripped.
    assert f"[{short}]" in explanation.narrative
    assert "long debate" not in explanation.narrative
    assert len(explanation.ungrounded_sentences) == 1
    assert "long debate" in explanation.ungrounded_sentences[0]
    assert explanation.backend_used == "llm"


@pytest.mark.asyncio
async def test_llm_narrator_falls_back_on_sdk_error(projection, base_ts) -> None:
    """When the LLM call raises, the narrator falls through to the
    templated path and returns a coherent grounded response."""
    root_id = _seed_chain(projection, base_ts=base_ts)

    from pocketpaw_ee.cloud.decisions.service import get_decision_graph

    graph = get_decision_graph()
    root = await graph.get(root_id)
    assert root is not None
    trace = await graph.trace(root_id, depth=2)

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=RuntimeError("api down"))

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        explanation = await narrate_decision(root, trace, backend="llm", api_key="sk-fake")

    assert explanation.backend_used == "templated"
    assert str(root_id)[:8] in explanation.narrative


# ---------------------------------------------------------------------------
# End-to-end orchestrator — find + trace + narrate + cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_orchestrator_runs_end_to_end(projection, base_ts) -> None:
    """A real question against a seeded chain produces a grounded
    narrative without touching an LLM (we force the templated backend)."""
    root_id = _seed_chain(
        projection,
        base_ts=base_ts,
        approver_user="prakash",
    )

    explanation = await explain(
        ExplainRequestInput(
            question="Why was lease LR-2026-117 approved on May 25?",
            backend="templated",
            max_decisions=3,
        ),
    )

    assert root_id in explanation.decisions_walked
    assert str(root_id)[:8] in explanation.narrative
    assert "Prakash" in explanation.narrative


# ---------------------------------------------------------------------------
# Cache — hit / miss + invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_cache_hits_on_second_call(graph, projection, base_ts) -> None:
    """First call populates the cache; second identical call returns
    the cached entry without re-narrating."""
    root_id = _seed_chain(projection, base_ts=base_ts)

    first = await explain(
        ExplainRequestInput(
            question="Why was LR-2026-117 approved?",
            backend="templated",
        ),
    )

    cache = get_explain_cache()
    assert cache.count() == 1

    # Mutate the narrative on the cached row so we can prove the second
    # call returned the cached one (without re-running narrator).
    from pocketpaw_ee.cloud.decisions.explain.cache import (
        _hash_scope,
        _normalize_question,
    )

    key = build_cache_key(
        question_normalized=_normalize_question("Why was LR-2026-117 approved?"),
        root_decision_id=root_id,
        depth=3,
        scope_hash=_hash_scope(None),
    )
    cached_explanation = cache.get(key)
    assert cached_explanation is not None
    cached_explanation.narrative = "SENTINEL CACHED VALUE"
    cache.put(
        key,
        cached_explanation,
        question_normalized=_normalize_question("Why was LR-2026-117 approved?"),
        root_decision_id=root_id,
        depth=3,
        scope_hash=_hash_scope(None),
    )

    second = await explain(
        ExplainRequestInput(
            question="Why was LR-2026-117 approved?",
            backend="templated",
        ),
    )
    assert second.narrative == "SENTINEL CACHED VALUE"
    assert first.decisions_walked == second.decisions_walked


@pytest.mark.asyncio
async def test_explain_cache_invalidated_by_new_event(graph, projection, base_ts) -> None:
    """When the projection folds a new event whose Decision is in a
    cached entry's `decisions_walked`, the cache entry is invalidated."""
    root_id = _seed_chain(projection, base_ts=base_ts)

    await explain(
        ExplainRequestInput(
            question="Why was LR-2026-117 approved?",
            backend="templated",
        ),
    )
    cache = get_explain_cache()
    assert cache.count() == 1

    # Now seed a NEW chain — this fires the projection's post-apply
    # hook, which the cache registered an invalidator with. The
    # invalidator drops rows whose `decisions_walked` includes the
    # newly-emitted decision. Our cached row's walked list is
    # [root_id]; the new decision is a different id, so the cache
    # row should survive. Let's first prove that.
    _seed_chain(projection, base_ts=base_ts + timedelta(hours=1))
    assert cache.count() == 1, "unrelated decision should not invalidate"

    # Now simulate the explicit invalidation of root_id and assert the
    # cache row is gone.
    removed = cache.invalidate_for_decisions([root_id])
    assert removed == 1
    assert cache.count() == 0


@pytest.mark.asyncio
async def test_explain_cache_invalidation_via_projection_hook(graph, projection, base_ts) -> None:
    """Confirm the cache invalidation hook is registered on the projection
    and fires when a new decision lands whose id matches a cached walked
    entry. We synthesise a chain where the new decision IS the cached
    decision (which requires re-emit through INSERT OR REPLACE)."""
    root_id = _seed_chain(projection, base_ts=base_ts)

    await explain(
        ExplainRequestInput(
            question="Why was LR-2026-117 approved?",
            backend="templated",
        ),
    )
    cache = get_explain_cache()
    assert cache.count() == 1

    # Manually fire the post-apply hooks with the SAME decision id —
    # mimics the cache-invalidation path the projection takes when an
    # outcome attaches to the existing decision (Slice 2 back-ref).
    root = await graph.get(root_id)
    assert root is not None
    projection._emit(root)
    # The hook should have wiped the cached row.
    assert cache.count() == 0


# ---------------------------------------------------------------------------
# Scope filtering — workspace A vs B
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_scope_filter_hides_other_workspace(graph, projection, base_ts) -> None:
    """A workspace-B caller can't reach workspace-A decisions through
    the explain orchestrator — `requester_scopes` is the gate."""
    _seed_chain(projection, base_ts=base_ts, workspace="ws_a_test")

    # B asks the same question — gets the empty-trace canned response
    # because the only matching decision is out of scope.
    response_b = await explain(
        ExplainRequestInput(
            question="Why was LR-2026-117 approved?",
            backend="templated",
        ),
        requester_scopes=["workspace:ws_b_test"],
    )
    assert response_b.decisions_walked == []
    assert "No matching decision was found" in response_b.narrative


# ---------------------------------------------------------------------------
# REST route — POST /api/v1/decisions/explain
# ---------------------------------------------------------------------------


def test_explain_route_returns_grounded_response(client: TestClient, projection, base_ts) -> None:
    """Round-trip through the FastAPI route. Templated backend so the
    test is hermetic."""
    root_id = _seed_chain(projection, base_ts=base_ts)
    resp = client.post(
        "/api/v1/decisions/explain",
        json={
            "question": "Why was LR-2026-117 approved on May 25?",
            "backend": "templated",
            "max_decisions": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert str(root_id) in body["decisions_walked"]
    assert str(root_id)[:8] in body["narrative"]
    assert body["backend_used"] == "templated"


def test_explain_route_rejects_empty_question(client: TestClient) -> None:
    """FastAPI validation rejects an empty question."""
    resp = client.post(
        "/api/v1/decisions/explain",
        json={"question": ""},
    )
    assert resp.status_code == 422


def test_explain_route_handles_no_match(client: TestClient) -> None:
    """No matching decision → 200 with the canned empty narrative
    and an empty decisions_walked list."""
    resp = client.post(
        "/api/v1/decisions/explain",
        json={
            "question": "Why was lease LR-9999-999 approved?",
            "backend": "templated",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decisions_walked"] == []
    assert "No matching decision was found" in body["narrative"]


def test_explain_response_dto_shape(projection, base_ts) -> None:
    """The wire shape matches what the frontend Slice 3b will consume.

    Sanity check on the round-trip ExplanationResponse.from_domain.
    """
    root_id = _seed_chain(projection, base_ts=base_ts)
    from pocketpaw_ee.cloud.decisions.explain.narrator import Explanation

    explanation = Explanation(
        narrative=f"It was approved [{str(root_id)[:8]}].",
        decisions_walked=[root_id],
        depth_reached=1,
        tokens_in=120,
        tokens_out=40,
        backend_used="llm",
    )
    wire = ExplanationResponse.from_domain(explanation)
    assert wire.narrative == explanation.narrative
    assert wire.decisions_walked == [root_id]
    assert wire.backend_used == "llm"
    # JSON round-trip — the wire shape is serialisable.
    data = wire.model_dump(mode="json")
    assert data["decisions_walked"] == [str(root_id)]
