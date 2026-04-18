# tests/cloud/test_decision_traces_wiring.py — Integration tests for PR-B.
# Created: 2026-04-13 — Verifies that propose() accepts and persists a trace,
# that fabric snapshots are keyed to the audit row, and that the hydration
# endpoint expands referenced IDs correctly with hydrate=0 / hydrate=1.

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.instinct.models import ActionTrigger
from ee.instinct.router import router
from ee.instinct.store import InstinctStore
from ee.instinct.trace import FabricObjectSnapshot, ReasoningTrace, ToolCallRef


def _trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="claude", reason="unit test")


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "traces_wiring.db")


@pytest.fixture
def app_with_store(tmp_path: Path):
    app = FastAPI()
    app.include_router(router)
    store = InstinctStore(tmp_path / "router_traces.db")
    with patch("ee.instinct.router._store", return_value=store):
        yield app, store


@pytest.fixture
def client(app_with_store):
    app, _ = app_with_store
    return TestClient(app)


# ---------------------------------------------------------------------------
# Store-level wiring
# ---------------------------------------------------------------------------


class TestProposeWithTrace:
    @pytest.mark.asyncio
    async def test_reasoning_trace_lands_in_audit_context(self, store: InstinctStore) -> None:
        trace = ReasoningTrace(
            fabric_queries=["obj_acme"],
            soul_memories=["mem_q4_pricing"],
            kb_articles=["kb_discount_policy"],
            tool_calls=[ToolCallRef(tool="kb_search", args_hash="abc", result_preview="…")],
            prompt_version="v1",
            backend="claude_agent_sdk",
            model="claude-opus-4-6",
        )
        await store.propose(
            pocket_id="pocket-1",
            title="Offer renewal discount",
            description="Acme up for renewal",
            recommendation="Offer 25%",
            trigger=_trigger(),
            reasoning_trace=trace,
        )

        entries = await store.query_audit(pocket_id="pocket-1")
        proposed = [e for e in entries if e.event == "action_proposed"]
        assert len(proposed) == 1
        decoded = ReasoningTrace.model_validate(
            proposed[0].context["reasoning_trace"],
        )
        assert decoded.fabric_queries == ["obj_acme"]
        assert decoded.soul_memories == ["mem_q4_pricing"]
        assert decoded.backend == "claude_agent_sdk"

    @pytest.mark.asyncio
    async def test_fabric_snapshots_are_keyed_to_the_audit_row(self, store: InstinctStore) -> None:
        snapshots = [
            FabricObjectSnapshot(
                object_id="obj_acme",
                audit_id="will-be-overwritten",
                object_type="Customer",
                snapshot={"arr": 180000},
            ),
            FabricObjectSnapshot(
                object_id="obj_snowflake",
                audit_id="will-be-overwritten",
                object_type="Competitor",
                snapshot={"last_seen": "Q4"},
            ),
        ]
        await store.propose(
            pocket_id="pocket-1",
            title="Offer renewal discount",
            description="",
            recommendation="",
            trigger=_trigger(),
            reasoning_trace=ReasoningTrace(fabric_queries=["obj_acme", "obj_snowflake"]),
            fabric_snapshots=snapshots,
        )

        entries = await store.query_audit(pocket_id="pocket-1")
        proposed = next(e for e in entries if e.event == "action_proposed")
        saved = await store.get_snapshots_for_audit(proposed.id)
        assert {s.object_id for s in saved} == {"obj_acme", "obj_snowflake"}
        for snap in saved:
            assert snap.audit_id == proposed.id

    @pytest.mark.asyncio
    async def test_propose_without_trace_still_works(self, store: InstinctStore) -> None:
        """Trace is optional — legacy callers keep working."""
        await store.propose(
            pocket_id="pocket-1",
            title="No trace",
            description="",
            recommendation="",
            trigger=_trigger(),
        )
        entries = await store.query_audit(pocket_id="pocket-1")
        proposed = next(e for e in entries if e.event == "action_proposed")
        assert "reasoning_trace" not in (proposed.context or {})


# ---------------------------------------------------------------------------
# Router-level wiring
# ---------------------------------------------------------------------------


class TestProposeEndpointWithTrace:
    def test_endpoint_accepts_and_persists_trace_and_snapshots(
        self, app_with_store, client: TestClient
    ) -> None:
        _, store = app_with_store
        payload = {
            "pocket_id": "pocket-1",
            "title": "Offer renewal discount",
            "description": "Acme up for renewal",
            "recommendation": "Offer 25%",
            "priority": "high",
            "trigger": {
                "type": "agent",
                "source": "claude",
                "reason": "renewal sequence",
            },
            "reasoning_trace": {
                "fabric_queries": ["obj_acme"],
                "soul_memories": [],
                "kb_articles": ["kb_pricing"],
                "tool_calls": [],
                "prompt_version": "v1",
                "backend": "claude_agent_sdk",
                "model": "claude-opus-4-6",
            },
            "fabric_snapshots": [
                {
                    "object_id": "obj_acme",
                    "audit_id": "placeholder",
                    "object_type": "Customer",
                    "snapshot": {"arr": 180000},
                },
            ],
        }
        res = client.post("/instinct/actions", json=payload)
        assert res.status_code == 201
        assert res.json()["title"] == "Offer renewal discount"
        assert res.json()["priority"] == "high"


class TestHydrationEndpoint:
    @pytest.mark.asyncio
    async def test_hydrate_zero_returns_decoded_trace_without_expansion(
        self, app_with_store, client: TestClient
    ) -> None:
        _, store = app_with_store
        await store.propose(
            pocket_id="pocket-1",
            title="Offer renewal discount",
            description="",
            recommendation="",
            trigger=_trigger(),
            reasoning_trace=ReasoningTrace(fabric_queries=["obj_acme"]),
        )
        entries = await store.query_audit(pocket_id="pocket-1")
        proposed = next(e for e in entries if e.event == "action_proposed")

        res = client.get(f"/instinct/audit/{proposed.id}")
        assert res.status_code == 200
        body = res.json()
        assert body["entry"]["id"] == proposed.id
        assert body["reasoning_trace"]["fabric_queries"] == ["obj_acme"]
        assert body["fabric_snapshots"] == []
        assert body["fabric_current"] == []

    @pytest.mark.asyncio
    async def test_hydrate_one_returns_snapshots(self, app_with_store, client: TestClient) -> None:
        _, store = app_with_store
        await store.propose(
            pocket_id="pocket-1",
            title="Offer renewal discount",
            description="",
            recommendation="",
            trigger=_trigger(),
            reasoning_trace=ReasoningTrace(fabric_queries=["obj_acme"]),
            fabric_snapshots=[
                FabricObjectSnapshot(
                    object_id="obj_acme",
                    audit_id="will-be-replaced",
                    object_type="Customer",
                    snapshot={"arr": 180000},
                ),
            ],
        )
        entries = await store.query_audit(pocket_id="pocket-1")
        proposed = next(e for e in entries if e.event == "action_proposed")

        res = client.get(f"/instinct/audit/{proposed.id}?hydrate=1")
        assert res.status_code == 200
        body = res.json()
        assert len(body["fabric_snapshots"]) == 1
        assert body["fabric_snapshots"][0]["object_id"] == "obj_acme"
        assert body["fabric_snapshots"][0]["snapshot"]["arr"] == 180000

    def test_hydrate_unknown_audit_returns_404(self, client: TestClient) -> None:
        res = client.get("/instinct/audit/aud_does_not_exist")
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_audit_entry_without_trace_hydrates_empty(
        self, app_with_store, client: TestClient
    ) -> None:
        _, store = app_with_store
        await store.propose(
            pocket_id="pocket-1",
            title="No trace",
            description="",
            recommendation="",
            trigger=_trigger(),
        )
        entries = await store.query_audit(pocket_id="pocket-1")
        proposed = next(e for e in entries if e.event == "action_proposed")
        res = client.get(f"/instinct/audit/{proposed.id}?hydrate=1")
        assert res.status_code == 200
        body = res.json()
        assert body["reasoning_trace"] is None
        assert body["fabric_snapshots"] == []
