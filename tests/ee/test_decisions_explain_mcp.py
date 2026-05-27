# tests/ee/test_decisions_explain_mcp.py — RFC 07 Slice 3a MCP coverage.
# Created: 2026-05-25 — pins the in-process MCP wrapper shipped in
#   `pocketpaw_ee.agent.mcp_servers.decisions` for the explain tool:
#
#     decisions_explain(question, scope=, max_decisions=, depth=, backend=)
#
#   The handler delegates to `pocketpaw_ee.cloud.decisions.explain.explain`
#   so the wire shape it returns mirrors the REST `ExplanationResponse`
#   exactly — REST and MCP must never drift. Test surface:
#
#     - Happy path: a real question produces a grounded narrative
#       envelope with `decisions_walked`.
#     - Identity: a missing workspace ContextVar yields the MCP error
#       envelope (the same chokepoint the other decisions_* handlers use).
#     - Validation: an empty question is an MCP error.
#     - Scope: a different workspace's decisions are invisible (same
#       silent-elide invariant the read tools enforce).
#     - Allowlist: the explain tool id is in the canonical
#       `DECISIONS_TOOL_IDS` tuple so the Claude Code allowlist machinery
#       picks it up.
"""Tests for the explain MCP wrapper."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pocketpaw_ee.agent.mcp_servers.decisions import (
    DECISIONS_EXPLAIN_TOOL_ID,
    DECISIONS_TOOL_IDS,
    _decisions_explain_handler,
)
from pocketpaw_ee.cloud.chat.agent_service import (
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.cloud.decisions.explain.cache import reset_explain_cache_for_tests
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import DecisionStore, set_db_path
from soul_protocol.spec.journal import Actor, EventEntry

# ---------------------------------------------------------------------------
# Fixtures — mirror test_decisions_mcp's pattern
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_projection_for_tests()
    reset_explain_cache_for_tests()
    yield
    reset_projection_for_tests()
    reset_explain_cache_for_tests()


@pytest.fixture
def workspace_id() -> str:
    return "ws_a_test"


@pytest.fixture
def agent_id() -> str:
    return "did:soul:test_agent"


@pytest.fixture
def _identity_context(workspace_id: str, agent_id: str):
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
    from pocketpaw_ee.cloud.decisions import service as decisions_service

    g = DecisionGraph(store=store, projection=projection)
    decisions_service._GRAPH = g
    return g


@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers — chain seeding, mirrors test_decisions_mcp._seed_chain
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
) -> UUID:
    corr = uuid4()
    scope = ["org:nerve", f"workspace:{workspace}", f"pocket:{pocket_id}"]
    actor = Actor(kind="agent", id=actor_id, scope_context=scope)
    payload: dict = {
        "intent": f"chain-{corr.hex[:8]}",
        "action": "send_to_tenant",
        "pocket_id": pocket_id,
        "inputs": [
            {"kind": "fabric_object", "id": "lease:LR-2026-117", "label": "Lease LR-2026-117"}
        ],
    }
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
    last_id: UUID | None = None
    for e in events:
        result = projection.apply(e)
        if result is not None:
            last_id = result.id
    assert last_id is not None
    return last_id


def _read_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    assert "is_error" not in envelope or envelope.get("is_error") is False
    return json.loads(envelope["content"][0]["text"])


# ---------------------------------------------------------------------------
# Happy path — the wrapper returns the explain wire shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_handler_returns_grounded_envelope(
    _identity_context, graph, projection, base_ts
) -> None:
    """End-to-end through the MCP wrapper: seeded decision, templated
    backend, wire envelope carries the same fields the REST response
    does."""
    root_id = _seed_chain(projection, base_ts=base_ts)

    envelope = await _decisions_explain_handler(
        {
            "question": "Why was LR-2026-117 approved?",
            "backend": "templated",
            "max_decisions": 3,
        }
    )
    body = _read_envelope(envelope)
    # Wire fields the REST ExplanationResponse defines.
    assert "narrative" in body
    assert "decisions_walked" in body
    assert "backend_used" in body
    assert body["backend_used"] == "templated"
    assert str(root_id) in body["decisions_walked"]
    assert str(root_id)[:8] in body["narrative"]


# ---------------------------------------------------------------------------
# Identity — missing workspace yields the MCP error envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_handler_requires_identity(graph) -> None:
    """Outside a chat stream, the workspace ContextVar is None and the
    handler returns the canonical "no active workspace" error."""
    envelope = await _decisions_explain_handler({"question": "Why was LR-2026-117 approved?"})
    assert envelope.get("is_error") is True
    assert "no active workspace" in envelope["content"][0]["text"]


# ---------------------------------------------------------------------------
# Validation — empty question is an error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_handler_rejects_empty_question(_identity_context, graph) -> None:
    envelope = await _decisions_explain_handler({"question": ""})
    assert envelope.get("is_error") is True
    assert "question is required" in envelope["content"][0]["text"]


@pytest.mark.asyncio
async def test_explain_handler_rejects_missing_question(_identity_context, graph) -> None:
    envelope = await _decisions_explain_handler({})
    assert envelope.get("is_error") is True


# ---------------------------------------------------------------------------
# Scope — other-workspace decisions are invisible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_handler_respects_scope(
    _identity_context, graph, projection, base_ts
) -> None:
    """A decision seeded under a different workspace is not visible
    through the explain wrapper — the canned "no matching decision"
    response comes back instead."""
    _seed_chain(projection, base_ts=base_ts, workspace="ws_other")

    envelope = await _decisions_explain_handler(
        {
            "question": "Why was LR-2026-117 approved?",
            "backend": "templated",
        }
    )
    body = _read_envelope(envelope)
    assert body["decisions_walked"] == []
    assert "No matching decision was found" in body["narrative"]


# ---------------------------------------------------------------------------
# Allowlist — tool id namespaces match the Claude Code convention
# ---------------------------------------------------------------------------


def test_explain_tool_id_in_allowlist() -> None:
    """The explain tool id must be in `DECISIONS_TOOL_IDS` so the
    backend's allowlist machinery picks it up alongside the read tools."""
    assert DECISIONS_EXPLAIN_TOOL_ID in DECISIONS_TOOL_IDS
    assert DECISIONS_EXPLAIN_TOOL_ID.startswith("mcp__pocketpaw_decisions__")
    # Namespace symmetry — every read tool id has the same prefix.
    for tool_id in DECISIONS_TOOL_IDS:
        assert tool_id.startswith("mcp__pocketpaw_decisions__")
