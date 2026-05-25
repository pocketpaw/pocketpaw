# tests/ee/test_decisions_mcp.py — RFC 07 Slice 2 MCP wrapper coverage.
# Created: 2026-05-25 — pins the three in-process MCP tool handlers
#   shipped in `pocketpaw_ee.agent.mcp_servers.decisions`:
#
#     decisions_get(decision_id)
#     decisions_find(...)
#     decisions_trace(decision_id, depth=3)
#
#   The handlers resolve identity via per-stream ContextVars from
#   `ee.cloud.chat.agent_service`. Tests set those ContextVars directly
#   so the handlers receive a workspace + agent id without spinning up
#   a real SSE chat stream.
#
#   The wrappers wire onto the same `DecisionGraph` Python API the REST
#   router uses, so we assert that the JSON envelope each handler
#   produces carries the wire shape `DecisionResponse` defines — REST
#   and MCP must never drift.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from soul_protocol.spec.journal import Actor, EventEntry

from pocketpaw_ee.agent.mcp_servers.decisions import (
    _decisions_find_handler,
    _decisions_get_handler,
    _decisions_trace_handler,
)
from pocketpaw_ee.cloud.chat.agent_service import (
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import DecisionStore, set_db_path

# ---------------------------------------------------------------------------
# Identity context — set the per-stream ContextVars the MCP handlers read
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_projection():
    reset_projection_for_tests()
    yield
    reset_projection_for_tests()


@pytest.fixture
def workspace_id() -> str:
    return "ws_a_test"


@pytest.fixture
def agent_id() -> str:
    return "did:soul:test_agent"


@pytest.fixture(autouse=True)
def _identity_context(workspace_id: str, agent_id: str):
    """Install workspace + agent ContextVars via the canonical helper
    so the MCP identity resolver returns real values instead of
    (None, None)."""
    tokens = attach_agent_identity(workspace_id=workspace_id, user_id=agent_id)
    yield
    detach_agent_identity(tokens)


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
    """Install a DecisionGraph singleton so the MCP wrappers resolve to it."""
    from pocketpaw_ee.cloud.decisions import service as decisions_service

    g = DecisionGraph(store=store, projection=projection)
    decisions_service._GRAPH = g
    return g


@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers — same chain-seeding helper as the router test, scoped here so
# the two files stay independent
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
    precedents: list[dict] | None = None,
) -> UUID:
    corr = uuid4()
    scope = ["org:nerve", f"workspace:{workspace}", f"pocket:{pocket_id}"]
    actor = Actor(kind="agent", id=actor_id, scope_context=scope)
    payload: dict = {
        "intent": f"chain-{corr.hex[:8]}",
        "action": "send_to_tenant",
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
            action="decision.graduated",
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


def _read_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Decode the MCP success envelope's JSON-encoded text content."""
    assert "is_error" not in envelope or envelope.get("is_error") is False
    text = envelope["content"][0]["text"]
    return json.loads(text)


# ---------------------------------------------------------------------------
# decisions_get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decisions_get_returns_decision(graph, projection, base_ts) -> None:
    decision_id = _seed_chain(projection, base_ts=base_ts)

    envelope = await _decisions_get_handler({"decision_id": str(decision_id)})
    body = _read_envelope(envelope)
    assert body["found"] is True
    assert body["decision"]["id"] == str(decision_id)
    # Wire shape — same fields as the REST DecisionResponse.
    assert body["decision"]["actor_kind"] == "agent"
    assert body["decision"]["actor_id"] == "did:soul:agent1"


@pytest.mark.asyncio
async def test_decisions_get_returns_not_found_envelope(graph) -> None:
    envelope = await _decisions_get_handler({"decision_id": str(uuid4())})
    body = _read_envelope(envelope)
    assert body["found"] is False
    assert body["decision"] is None


@pytest.mark.asyncio
async def test_decisions_get_returns_error_on_missing_id(graph) -> None:
    envelope = await _decisions_get_handler({})
    assert envelope.get("is_error") is True
    assert "decision_id is required" in envelope["content"][0]["text"]


@pytest.mark.asyncio
async def test_decisions_get_returns_error_on_bad_uuid(graph) -> None:
    envelope = await _decisions_get_handler({"decision_id": "not-a-uuid"})
    assert envelope.get("is_error") is True
    assert "not a valid UUID" in envelope["content"][0]["text"]


@pytest.mark.asyncio
async def test_decisions_get_respects_scope(graph, projection, base_ts) -> None:
    """A decision in another workspace's scope returns the not-found envelope."""
    decision_id = _seed_chain(projection, base_ts=base_ts, workspace="ws_other")
    envelope = await _decisions_get_handler({"decision_id": str(decision_id)})
    body = _read_envelope(envelope)
    assert body["found"] is False


# ---------------------------------------------------------------------------
# decisions_find
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decisions_find_returns_list(graph, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts, actor_id="did:soul:a")
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=10),
        actor_id="did:soul:b",
    )

    envelope = await _decisions_find_handler({})
    body = _read_envelope(envelope)
    assert body["count"] == 2
    assert len(body["decisions"]) == 2


@pytest.mark.asyncio
async def test_decisions_find_filters_by_actor(graph, projection, base_ts) -> None:
    _seed_chain(projection, base_ts=base_ts, actor_id="did:soul:alpha")
    _seed_chain(
        projection,
        base_ts=base_ts + timedelta(seconds=10),
        actor_id="did:soul:beta",
    )

    envelope = await _decisions_find_handler({"actor": "did:soul:alpha"})
    body = _read_envelope(envelope)
    assert body["count"] == 1
    assert body["decisions"][0]["actor_id"] == "did:soul:alpha"


@pytest.mark.asyncio
async def test_decisions_find_respects_scope(graph, projection, base_ts) -> None:
    """Find returns no results when every decision is out of scope."""
    _seed_chain(projection, base_ts=base_ts, workspace="ws_other")
    envelope = await _decisions_find_handler({})
    body = _read_envelope(envelope)
    assert body["count"] == 0


# ---------------------------------------------------------------------------
# decisions_trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decisions_trace_walks_precedents(graph, projection, base_ts) -> None:
    prec_id = _seed_chain(projection, base_ts=base_ts - timedelta(days=1))
    new_id = _seed_chain(
        projection,
        base_ts=base_ts,
        precedents=[{"decision_id": str(prec_id), "weight": 0.9}],
    )

    envelope = await _decisions_trace_handler(
        {"decision_id": str(new_id), "depth": 2}
    )
    body = _read_envelope(envelope)
    assert body["root"] == str(new_id)
    assert str(prec_id) in body["nodes"]


@pytest.mark.asyncio
async def test_decisions_trace_returns_error_on_missing_id(graph) -> None:
    envelope = await _decisions_trace_handler({})
    assert envelope.get("is_error") is True


@pytest.mark.asyncio
async def test_decisions_trace_returns_error_on_bad_uuid(graph) -> None:
    envelope = await _decisions_trace_handler({"decision_id": "not-a-uuid"})
    assert envelope.get("is_error") is True


# ---------------------------------------------------------------------------
# Identity / surface contract
# ---------------------------------------------------------------------------


def test_tool_ids_namespace_consistently() -> None:
    """Tool ids must namespace to `mcp__pocketpaw_decisions__<verb>` so
    the Claude Code allowlist machinery matches them."""
    from pocketpaw_ee.agent.mcp_servers.decisions import DECISIONS_TOOL_IDS

    for tool_id in DECISIONS_TOOL_IDS:
        assert tool_id.startswith("mcp__pocketpaw_decisions__")
