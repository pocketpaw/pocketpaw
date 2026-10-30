# tests/ee/test_outcomes_decision_backref.py — RFC 07 Slice 2 outcomes
# back-reference. Pins the decision-graph back-reference flow:
#
#   1. Seed a Decision via the projection (its row lands with no outcome).
#   2. Call `outcomes.service.record_outcome` with a `pocket.outcome`
#      event whose payload carries the `decision_id`.
#   3. Assert the decision row's `outcome` field is now populated AND
#      the ledger row carries the back-reference.
#
# The DecisionProjection's `_apply_outcome_attached` handler is the load-
# bearing piece — `record_outcome` synthesises a `decision.outcome_attached`
# journal-shaped EventEntry and feeds it through `projection.apply()`.
# That keeps the back-reference flow strictly in-process; the
# journal-to-projection subscription that would do the same end-to-end
# is deferred to a follow-up.
#
# Created: 2026-05-25 (RFC 07 Slice 2).

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import DecisionStore, set_db_path
from pocketpaw_ee.cloud.outcomes import service as outcomes_service
from pocketpaw_ee.cloud.outcomes.dto import OutcomeResponse
from soul_protocol.spec.journal import Actor, EventEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_projection():
    reset_projection_for_tests()
    yield
    reset_projection_for_tests()


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
    """Install the DecisionGraph singleton — outcomes service resolves
    through `get_decision_graph()` so this is the test seam."""
    from pocketpaw_ee.cloud.decisions import service as decisions_service

    g = DecisionGraph(store=store, projection=projection)
    decisions_service._GRAPH = g
    return g


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    """Point the outcomes ledger at a tmp directory so tests never
    touch the real ~/.pocketpaw/outcomes/ tree."""
    d = tmp_path / "outcomes"
    outcomes_service.set_ledger_dir(d)
    yield d
    # Restore the default after each test so other tests on the same
    # process aren't redirected.
    outcomes_service.set_ledger_dir(Path.home() / ".pocketpaw" / "outcomes")


@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_decision(
    projection: DecisionProjection,
    *,
    base_ts: datetime,
    workspace: str = "ws_a_test",
    pocket_id: str = "p_main",
) -> UUID:
    """Seed one approval chain and return the Decision's id."""
    corr = uuid4()
    scope = ["org:nerve", f"workspace:{workspace}", f"pocket:{pocket_id}"]
    actor = Actor(kind="agent", id="did:soul:agent_decisions", scope_context=scope)
    events = [
        EventEntry(
            id=uuid4(),
            ts=base_ts,
            actor=actor,
            action="agent.proposed",
            scope=scope,
            correlation_id=corr,
            payload={
                "intent": "test back-reference",
                "action": "send_to_tenant",
                "pocket_id": pocket_id,
            },
        ),
        EventEntry(
            id=uuid4(),
            ts=base_ts + timedelta(seconds=1),
            actor=actor,
            action="decision.completed",
            scope=scope,
            correlation_id=corr,
            payload={"passed": True},
        ),
    ]
    last_decision_id: UUID | None = None
    for e in events:
        result = projection.apply(e)
        if result is not None:
            last_decision_id = result.id
    assert last_decision_id is not None
    return last_decision_id


class _FakeEvent:
    """Tiny stand-in for `PocketOutcomeEvent` — the listener reads
    `.data` only, so a duck type is sufficient and keeps the test free
    of bus wiring concerns."""

    def __init__(self, data: dict) -> None:
        self.data = data


# ---------------------------------------------------------------------------
# Outcome → Decision back-reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_outcome_attaches_outcome_to_decision(
    graph, projection, ledger_dir: Path, base_ts: datetime
) -> None:
    """A `pocket.outcome` event carrying a `decision_id` mutates the
    Decision row's outcome field in place."""
    decision_id = _seed_decision(projection, base_ts=base_ts)

    # Sanity — Decision starts with no outcome.
    pre = graph.store.get_decision(decision_id)
    assert pre is not None
    assert pre.outcome is None

    event = _FakeEvent(
        data={
            "outcome": "lease_renewed",
            "pocket_id": "p_main",
            "workspace_id": "ws_a_test",
            "action": "send_to_tenant",
            "actor": "did:soul:agent_decisions",
            "via_instinct": False,
            "instinct_action_id": None,
            "occurred_at": (base_ts + timedelta(seconds=5)).isoformat(),
            "outcome_value": None,
            "outcome_unit": None,
            "decision_id": str(decision_id),
        }
    )

    await outcomes_service.record_outcome(event)

    # The Decision row now carries the outcome (status=landed) — projection
    # folded the synthetic `decision.outcome_attached` event.
    post = graph.store.get_decision(decision_id)
    assert post is not None
    assert post.outcome is not None
    assert post.outcome.status == "landed"


@pytest.mark.asyncio
async def test_record_outcome_writes_decision_id_to_ledger(
    graph, projection, ledger_dir: Path, base_ts: datetime
) -> None:
    """The ledger JSONL row mirrors the back-reference."""
    decision_id = _seed_decision(projection, base_ts=base_ts)

    event = _FakeEvent(
        data={
            "outcome": "lease_renewed",
            "pocket_id": "p_main",
            "workspace_id": "ws_a_test",
            "action": "send_to_tenant",
            "actor": "did:soul:agent_decisions",
            "via_instinct": False,
            "instinct_action_id": None,
            "occurred_at": (base_ts + timedelta(seconds=5)).isoformat(),
            "outcome_value": None,
            "outcome_unit": None,
            "decision_id": str(decision_id),
        }
    )

    await outcomes_service.record_outcome(event)

    ledger_path = ledger_dir / "ws_a_test.jsonl"
    assert ledger_path.exists()
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["decision_id"] == str(decision_id)
    assert rows[0]["outcome"] == "lease_renewed"


@pytest.mark.asyncio
async def test_record_outcome_without_decision_id_is_legacy_safe(
    graph, projection, ledger_dir: Path, base_ts: datetime
) -> None:
    """A producer that doesn't pass `decision_id` still meters the
    outcome — the back-reference is just absent. Legacy writers keep
    working."""
    event = _FakeEvent(
        data={
            "outcome": "tenant_replied",
            "pocket_id": "p_main",
            "workspace_id": "ws_a_test",
            "action": "send_to_tenant",
            "actor": "did:soul:agent_decisions",
            "via_instinct": False,
            "instinct_action_id": None,
            "occurred_at": (base_ts + timedelta(seconds=5)).isoformat(),
            "outcome_value": None,
            "outcome_unit": None,
            # decision_id omitted on purpose.
        }
    )

    await outcomes_service.record_outcome(event)

    ledger_path = ledger_dir / "ws_a_test.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line]
    assert len(rows) == 1
    assert rows[0].get("decision_id") is None


@pytest.mark.asyncio
async def test_record_outcome_with_bad_decision_id_skips_attach(
    graph, projection, ledger_dir: Path, base_ts: datetime
) -> None:
    """An invalid `decision_id` is logged + ignored — the ledger row
    still lands so the outcome meter doesn't regress."""
    event = _FakeEvent(
        data={
            "outcome": "lease_renewed",
            "pocket_id": "p_main",
            "workspace_id": "ws_a_test",
            "action": "send_to_tenant",
            "actor": "did:soul:agent_decisions",
            "via_instinct": False,
            "instinct_action_id": None,
            "occurred_at": (base_ts + timedelta(seconds=5)).isoformat(),
            "decision_id": "not-a-uuid",
        }
    )

    # No exception even with a bad id.
    await outcomes_service.record_outcome(event)

    ledger_path = ledger_dir / "ws_a_test.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_emit_pocket_outcome_threads_decision_id(monkeypatch) -> None:
    """`emit_pocket_outcome` forwards the new `decision_id` parameter
    onto the `pocket.outcome` bus event."""
    captured: dict = {}

    async def fake_emit(event):  # noqa: ANN001
        captured["data"] = dict(event.data)

    monkeypatch.setattr("pocketpaw_ee.cloud.outcomes.service.emit", fake_emit)

    await outcomes_service.emit_pocket_outcome(
        outcome="lease_renewed",
        pocket_id="p_main",
        workspace_id="ws_a_test",
        action="send_to_tenant",
        actor="did:soul:agent_decisions",
        via_instinct=False,
        decision_id="abc-decision-id",
    )

    assert captured["data"]["decision_id"] == "abc-decision-id"
    assert captured["data"]["outcome"] == "lease_renewed"


def test_outcome_response_carries_decision_id() -> None:
    """Wire shape exposes the back-reference."""
    resp = OutcomeResponse(
        outcome="lease_renewed",
        pocket_id="p_main",
        workspace_id="ws_a_test",
        action="send_to_tenant",
        actor="did:soul:agent_decisions",
        via_instinct=False,
        occurred_at="2026-05-25T12:00:00+00:00",
        decision_id="abc-decision-id",
    )
    body = resp.model_dump()
    assert body["decision_id"] == "abc-decision-id"
