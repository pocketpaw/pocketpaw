# tests/ee/test_decision_projection.py — Coverage for the RFC 07 Slice 1
# Decision projection. These tests pin the invariants the substrate is
# supposed to hold; if any of them regress we've silently violated the
# A1/A2/A3 amendments or the hash-chain G6 contract.
#
# Created: 2026-05-25 (RFC 07 Slice 1, feat/rfc07-decision-graph-slice1).
#
# Mirrors `tests/ee/test_fabric_journal.py` fixture style: a fresh store
# per test via `tmp_path` + `set_db_path`, then run events through the
# projection directly. We don't open a real soul-protocol Journal here —
# the projection's contract is `apply(EventEntry)` and we test that
# contract by handing it constructed EventEntry instances. The
# journal-to-projection subscription that produces those entries from a
# real journal lands in Slice 2.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pocketpaw_ee.cloud.decisions.domain import ApproverRef, compute_hash_link
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.store import DecisionStore, set_db_path
from soul_protocol.spec.journal import Actor, EventEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> DecisionStore:
    """Fresh SQLite-backed store per test."""
    set_db_path(tmp_path / "decisions.db")
    s = DecisionStore()
    yield s
    s.close()


@pytest.fixture
def projection(store: DecisionStore) -> DecisionProjection:
    return DecisionProjection(store=store)


@pytest.fixture
def base_ts() -> datetime:
    """Stable base timestamp for ordered event chains. tz-aware UTC per
    EventEntry's validator."""
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers — build EventEntry instances for the projection
# ---------------------------------------------------------------------------


def _agent_actor(agent_id: str = "did:soul:lease_specialist") -> Actor:
    return Actor(kind="agent", id=agent_id, scope_context=["org:nerve"])


def _user_actor(user_id: str = "user:prakash") -> Actor:
    return Actor(kind="user", id=user_id, scope_context=["org:nerve"])


def _system_actor(system_id: str = "system:instinct") -> Actor:
    return Actor(kind="system", id=system_id, scope_context=["org:nerve"])


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
        scope=scope or ["org:nerve", "pocket:p_lease_renewals"],
        correlation_id=correlation_id,
        payload=payload,
        seq=seq,
    )


def _approval_chain(
    base_ts: datetime,
    correlation_id: UUID | None = None,
    *,
    include_fabric_write: bool = True,
    scope: list[str] | None = None,
    pocket_id: str = "p_lease_renewals",
) -> tuple[UUID, list[EventEntry]]:
    """Build a 5-event approval chain ending in `decision.graduated`."""
    corr = correlation_id or uuid4()
    scope = scope or ["org:nerve", f"pocket:{pocket_id}"]
    events = [
        _event(
            seq=100,
            ts=base_ts,
            actor=_agent_actor(),
            action="agent.proposed",
            correlation_id=corr,
            payload={
                "intent": "Renew LR-2026-117 at $2,850",
                "action": "send_to_tenant",
                "pocket_id": pocket_id,
                "inputs": [
                    {"kind": "fabric_object", "id": "lease:LR-2026-117"},
                    {"kind": "fabric_object", "id": "tenant:t_42"},
                ],
                "data": {"rent_old": 2750, "rent_new": 2850},
            },
            scope=scope,
        ),
        _event(
            seq=101,
            ts=base_ts + timedelta(seconds=1),
            actor=_system_actor(),
            action="policy.evaluated",
            correlation_id=corr,
            payload={
                "policy": "approve_per_row",
                "passed": False,
                "reason": "human approval required",
            },
            scope=scope,
        ),
        _event(
            seq=102,
            ts=base_ts + timedelta(seconds=10),
            actor=_user_actor(),
            action="human.corrected",
            correlation_id=corr,
            payload={"action": "approve", "note": "comp set checks out"},
            scope=scope,
        ),
        _event(
            seq=103,
            ts=base_ts + timedelta(seconds=11),
            actor=_system_actor(),
            action="policy.evaluated",
            correlation_id=corr,
            payload={"policy": "approve_per_row", "passed": True},
            scope=scope,
        ),
    ]
    if include_fabric_write:
        events.append(
            _event(
                seq=104,
                ts=base_ts + timedelta(seconds=14),
                actor=_agent_actor(),
                action="fabric.object.updated",
                correlation_id=corr,
                payload={
                    "object_id": "lease:LR-2026-117",
                    "properties": {"status": "renewal_sent"},
                },
                scope=scope,
            )
        )
    events.append(
        _event(
            seq=105,
            ts=base_ts + timedelta(seconds=15),
            actor=_agent_actor(),
            action="decision.graduated",
            correlation_id=corr,
            payload={"passed": True},
            scope=scope,
        )
    )
    return corr, events


# ---------------------------------------------------------------------------
# 1. Happy-path: 5-event chain emits one Decision on `decision.graduated`
# ---------------------------------------------------------------------------


def test_chain_emits_decision_on_graduated(
    projection: DecisionProjection, base_ts: datetime
) -> None:
    """A canonical chain (proposed → policy → human → policy → fabric →
    graduated) emits exactly one Decision, and the terminal event is the
    graduated event."""
    _, events = _approval_chain(base_ts)

    emitted = [d for d in (projection.apply(e) for e in events) if d is not None]
    assert len(emitted) == 1
    decision = emitted[0]

    # The Decision's action comes from the proposed payload, not the
    # fabric write.
    assert decision.action == "send_to_tenant"
    assert decision.intent.startswith("Renew LR-2026-117")
    # Policy ended on `passed=true` — outcome is None (not rejected).
    assert decision.instinct_policy == "approve_per_row"
    assert decision.instinct_policy_passed is True
    assert decision.outcome is None
    # Approvers landed as ApproverRef with approved_at.
    assert len(decision.approvers) == 1
    assert decision.approvers[0].approved_at == base_ts + timedelta(seconds=10)
    # Fabric write contributed the target object as an input.
    input_ids = {i.id for i in decision.inputs}
    assert "lease:LR-2026-117" in input_ids
    assert "tenant:t_42" in input_ids
    # The store has exactly one row.
    assert projection.store.count() == 1


# ---------------------------------------------------------------------------
# 2. A2 — Fabric writes contribute, do NOT close the chain
# ---------------------------------------------------------------------------


def test_fabric_writes_do_not_close_chain(
    projection: DecisionProjection, base_ts: datetime
) -> None:
    """A2 enforcement: a fabric.object.updated event mid-flow records the
    target object as an input but the chain stays OPEN until
    decision.graduated lands. The store stays empty until then."""
    corr = uuid4()
    events_before_graduated = [
        _event(
            seq=200,
            ts=base_ts,
            actor=_agent_actor(),
            action="agent.proposed",
            correlation_id=corr,
            payload={
                "intent": "Patch the lease",
                "action": "patch",
                "pocket_id": "p_lease_renewals",
            },
        ),
        _event(
            seq=201,
            ts=base_ts + timedelta(seconds=1),
            actor=_agent_actor(),
            action="fabric.object.updated",
            correlation_id=corr,
            payload={"object_id": "lease:LR-2026-117"},
        ),
    ]
    for e in events_before_graduated:
        result = projection.apply(e)
        assert result is None, f"chain emitted prematurely on {e.action}"
    # Still empty.
    assert projection.store.count() == 0

    # Now graduate.
    graduated = _event(
        seq=202,
        ts=base_ts + timedelta(seconds=2),
        actor=_agent_actor(),
        action="decision.graduated",
        correlation_id=corr,
        payload={"passed": True},
    )
    decision = projection.apply(graduated)
    assert decision is not None
    assert decision.action == "patch"
    # The fabric write's target object is present as an input.
    assert any(i.id == "lease:LR-2026-117" for i in decision.inputs)
    assert projection.store.count() == 1


# ---------------------------------------------------------------------------
# 3. Rejection chain — `policy.evaluated(passed=false)` → graduated
# ---------------------------------------------------------------------------


def test_rejection_chain_emits_rejected_outcome(
    projection: DecisionProjection, base_ts: datetime
) -> None:
    """A rejected policy followed by decision.graduated emits a Decision
    with outcome.status == 'rejected'."""
    corr = uuid4()
    events = [
        _event(
            seq=300,
            ts=base_ts,
            actor=_agent_actor(),
            action="agent.proposed",
            correlation_id=corr,
            payload={
                "intent": "Send tenant a renewal",
                "action": "send_renewal",
                "pocket_id": "p_lease_renewals",
            },
        ),
        _event(
            seq=301,
            ts=base_ts + timedelta(seconds=1),
            actor=_system_actor(),
            action="policy.evaluated",
            correlation_id=corr,
            payload={
                "policy": "approve_per_row",
                "passed": False,
                "reason": "rent above market median",
            },
        ),
        _event(
            seq=302,
            ts=base_ts + timedelta(seconds=2),
            actor=_agent_actor(),
            action="decision.graduated",
            correlation_id=corr,
            payload={"passed": False},
        ),
    ]
    emitted = [d for d in (projection.apply(e) for e in events) if d is not None]
    assert len(emitted) == 1
    decision = emitted[0]
    assert decision.outcome is not None
    assert decision.outcome.status == "rejected"
    assert decision.instinct_policy_passed is False
    # Rejection reason landed in the payload (for the narrator).
    assert decision.payload.get("rejection_reason") == "rent above market median"


# ---------------------------------------------------------------------------
# 4. A1 — ApproverRef carries approved_at + position
# ---------------------------------------------------------------------------


def test_approver_ref_carries_timestamp_and_position(
    projection: DecisionProjection, base_ts: datetime
) -> None:
    """A1 enforcement: every approver lands as an ApproverRef with the
    `human.corrected` event's ts and a position index starting at 0."""
    corr = uuid4()
    approver_one = _user_actor("user:prakash")
    approver_two = _user_actor("user:robert")
    events = [
        _event(
            seq=400,
            ts=base_ts,
            actor=_agent_actor(),
            action="agent.proposed",
            correlation_id=corr,
            payload={"intent": "x", "action": "patch", "pocket_id": "p"},
        ),
        _event(
            seq=401,
            ts=base_ts + timedelta(seconds=5),
            actor=approver_one,
            action="human.corrected",
            correlation_id=corr,
            payload={"action": "approve"},
        ),
        _event(
            seq=402,
            ts=base_ts + timedelta(seconds=10),
            actor=approver_two,
            action="human.corrected",
            correlation_id=corr,
            payload={"action": "approve"},
        ),
        _event(
            seq=403,
            ts=base_ts + timedelta(seconds=12),
            actor=_agent_actor(),
            action="decision.graduated",
            correlation_id=corr,
            payload={"passed": True},
        ),
    ]
    emitted = [d for d in (projection.apply(e) for e in events) if d is not None]
    assert len(emitted) == 1
    decision = emitted[0]
    assert len(decision.approvers) == 2
    first, second = decision.approvers
    assert isinstance(first, ApproverRef)
    assert first.actor.id == "user:prakash"
    assert first.approved_at == base_ts + timedelta(seconds=5)
    assert first.position == 0
    assert second.actor.id == "user:robert"
    assert second.position == 1


# ---------------------------------------------------------------------------
# 5. G6 — hash chain includes prev_hash within correlation_id
# ---------------------------------------------------------------------------


def test_hash_chain_includes_prev_hash(projection: DecisionProjection, base_ts: datetime) -> None:
    """Two decisions in the same correlation_id form a hash chain: the
    second's hash_link depends on the first. Recomputing the second's
    hash with the first's hash as prev produces the stored value."""
    # First chain.
    corr = uuid4()
    _, events_one = _approval_chain(base_ts, correlation_id=corr)
    emitted_one = [d for d in (projection.apply(e) for e in events_one) if d is not None]
    assert len(emitted_one) == 1
    first = emitted_one[0]

    # Second chain — same correlation_id, later ts.
    later = base_ts + timedelta(minutes=10)
    _, events_two = _approval_chain(later, correlation_id=corr)
    # Bump seq to keep the cursor monotonic.
    for i, e in enumerate(events_two):
        events_two[i] = e.model_copy(update={"seq": 1000 + i})
    emitted_two = [d for d in (projection.apply(e) for e in events_two) if d is not None]
    assert len(emitted_two) == 1
    second = emitted_two[0]

    # Sanity: distinct ids, same correlation.
    assert first.id != second.id
    assert first.correlation_id == second.correlation_id == corr
    # Recompute the second hash using the first's hash as prev — must match.
    expected = compute_hash_link(second, first.hash_link)
    assert second.hash_link == expected
    # And explicitly: the second hash is NOT the same as if prev were empty.
    no_prev = compute_hash_link(second, "")
    assert second.hash_link != no_prev


# ---------------------------------------------------------------------------
# 6. `decision.outcome_attached` does not affect the hash
# ---------------------------------------------------------------------------


def test_outcome_attached_does_not_affect_hash(
    projection: DecisionProjection, base_ts: datetime
) -> None:
    """Late `decision.outcome_attached` mutates the outcome columns but
    leaves the hash_link bit-identical (outcome is excluded from the
    hash material — that's what makes outcomes safe to amend)."""
    _, events = _approval_chain(base_ts)
    emitted = [d for d in (projection.apply(e) for e in events) if d is not None]
    assert len(emitted) == 1
    decision = emitted[0]
    original_hash = decision.hash_link

    # Attach an outcome.
    attach = _event(
        seq=999,
        ts=base_ts + timedelta(minutes=1),
        actor=_system_actor("system:outcome"),
        action="decision.outcome_attached",
        correlation_id=decision.correlation_id,
        payload={
            "decision_id": str(decision.id),
            "outcome_id": str(uuid4()),
            "status": "landed",
            "metered": True,
            "landed_at": (base_ts + timedelta(minutes=1)).isoformat(),
        },
    )
    refreshed = projection.apply(attach)
    assert refreshed is not None
    assert refreshed.outcome is not None
    assert refreshed.outcome.status == "landed"
    assert refreshed.outcome.metered is True
    # The hash MUST be identical — the chain stays valid.
    assert refreshed.hash_link == original_hash


# ---------------------------------------------------------------------------
# 7. A3 path 1 — agent-supplied precedents in proposed payload
# ---------------------------------------------------------------------------


def test_precedent_payload_supplied(projection: DecisionProjection, base_ts: datetime) -> None:
    """When the proposed payload carries `precedents`, those are kept
    as-is (no fallback). Two refs with explicit weights."""
    corr = uuid4()
    prec_a, prec_b = uuid4(), uuid4()
    events = [
        _event(
            seq=500,
            ts=base_ts,
            actor=_agent_actor(),
            action="agent.proposed",
            correlation_id=corr,
            payload={
                "intent": "Renew with precedents",
                "action": "send_to_tenant",
                "pocket_id": "p_lease_renewals",
                "precedents": [
                    {"decision_id": str(prec_a), "weight": 0.92},
                    {"decision_id": str(prec_b), "weight": 0.71},
                ],
            },
        ),
        _event(
            seq=501,
            ts=base_ts + timedelta(seconds=1),
            actor=_agent_actor(),
            action="decision.graduated",
            correlation_id=corr,
            payload={"passed": True},
        ),
    ]
    emitted = [d for d in (projection.apply(e) for e in events) if d is not None]
    assert len(emitted) == 1
    decision = emitted[0]
    assert len(decision.precedents) == 2
    weights = {p.weight for p in decision.precedents}
    assert weights == {0.92, 0.71}
    decision_ids = {p.decision_id for p in decision.precedents}
    assert decision_ids == {prec_a, prec_b}


# ---------------------------------------------------------------------------
# 8. A3 path 2 — fallback to same-pocket / same-action / nearest-ts
# ---------------------------------------------------------------------------


def test_precedent_fallback_same_pocket_action(
    projection: DecisionProjection, base_ts: datetime
) -> None:
    """When the proposed payload supplies no precedents, the projection
    falls back to same-pocket + same-action + top-3 nearest-ts. Decay
    weights: 0.95, 0.85, 0.75."""
    # Seed four prior decisions in the same pocket, same action.
    for i in range(4):
        corr_seed = uuid4()
        ts_seed = base_ts - timedelta(days=4 - i)
        events = [
            _event(
                seq=600 + i * 10,
                ts=ts_seed,
                actor=_agent_actor(),
                action="agent.proposed",
                correlation_id=corr_seed,
                payload={
                    "intent": f"seed-{i}",
                    "action": "send_to_tenant",
                    "pocket_id": "p_lease_renewals",
                },
            ),
            _event(
                seq=601 + i * 10,
                ts=ts_seed + timedelta(seconds=1),
                actor=_agent_actor(),
                action="decision.graduated",
                correlation_id=corr_seed,
                payload={"passed": True},
            ),
        ]
        for e in events:
            projection.apply(e)
    assert projection.store.count() == 4

    # Now a new chain with NO payload precedents.
    corr = uuid4()
    new_events = [
        _event(
            seq=700,
            ts=base_ts,
            actor=_agent_actor(),
            action="agent.proposed",
            correlation_id=corr,
            payload={
                "intent": "new renewal",
                "action": "send_to_tenant",
                "pocket_id": "p_lease_renewals",
            },
        ),
        _event(
            seq=701,
            ts=base_ts + timedelta(seconds=1),
            actor=_agent_actor(),
            action="decision.graduated",
            correlation_id=corr,
            payload={"passed": True},
        ),
    ]
    emitted = [d for d in (projection.apply(e) for e in new_events) if d is not None]
    assert len(emitted) == 1
    decision = emitted[0]
    # Top 3 nearest-ts siblings, decayed weights.
    assert len(decision.precedents) == 3
    weights = [p.weight for p in decision.precedents]
    assert weights == [0.95, 0.85, 0.75]


# ---------------------------------------------------------------------------
# 9. Rebuild idempotence — replay from genesis matches incremental state
# ---------------------------------------------------------------------------


def test_rebuild_idempotent(projection: DecisionProjection, base_ts: datetime) -> None:
    """Apply N events, snapshot store state, reset, apply the same N
    events via rebuild(since_seq=0), state must match."""
    _, events_one = _approval_chain(base_ts)
    _, events_two = _approval_chain(base_ts + timedelta(minutes=5))
    all_events = events_one + events_two
    for e in all_events:
        projection.apply(e)
    initial_count = projection.store.count()
    initial_decisions = sorted(
        ((d.intent, d.action, d.correlation_id) for d in projection.store.iter_decisions()),
        key=lambda t: str(t[2]),
    )
    assert initial_count == 2

    # Rebuild from genesis with the same events.
    projection.rebuild(iter(all_events), since_seq=0)
    after_count = projection.store.count()
    after_decisions = sorted(
        ((d.intent, d.action, d.correlation_id) for d in projection.store.iter_decisions()),
        key=lambda t: str(t[2]),
    )
    assert after_count == initial_count
    # Ids will differ (uuid4() per emission) but intent/action/correlation match.
    assert after_decisions == initial_decisions


# ---------------------------------------------------------------------------
# 10. Scope filter post-count invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_filter_post_count_invariant(
    projection: DecisionProjection, base_ts: datetime
) -> None:
    """A workspace-A query returns 0 workspace-B decisions. The count
    matches the filtered set — never the unfiltered count. Same shape
    as FabricProjection's pagination-leak fix."""
    from pocketpaw_ee.cloud.decisions.service import DecisionGraph

    # Two chains in different workspace scopes.
    _, events_a = _approval_chain(
        base_ts,
        scope=["org:nerve", "workspace:a"],
        pocket_id="pa",
    )
    _, events_b = _approval_chain(
        base_ts + timedelta(minutes=1),
        scope=["org:nerve", "workspace:b"],
        pocket_id="pb",
    )
    for e in events_a + events_b:
        projection.apply(e)

    # Store has 2 decisions total — unfiltered.
    assert projection.store.count() == 2

    graph = DecisionGraph(store=projection.store, projection=projection)
    # Workspace-A scope sees only workspace-A decisions.
    a_only = await graph.find(requester_scopes=["workspace:a"])
    assert len(a_only) == 1
    assert "workspace:a" in a_only[0].scope

    # Workspace-B scope sees only workspace-B decisions.
    b_only = await graph.find(requester_scopes=["workspace:b"])
    assert len(b_only) == 1
    assert "workspace:b" in b_only[0].scope

    # No-scope (admin) sees both.
    admin = await graph.find(requester_scopes=None)
    assert len(admin) == 2


# ---------------------------------------------------------------------------
# 11. Degenerate decision — orphan write event with no correlation
# ---------------------------------------------------------------------------


def test_degenerate_decision_no_correlation(
    projection: DecisionProjection, base_ts: datetime
) -> None:
    """A fabric.object.* event with no correlation_id (admin direct write)
    still emits a Decision so the graph has a node for it."""
    event = _event(
        seq=800,
        ts=base_ts,
        actor=_user_actor("user:admin"),
        action="fabric.object.updated",
        correlation_id=None,
        payload={"object_id": "lease:LR-2026-999"},
    )
    decision = projection.apply(event)
    assert decision is not None
    assert decision.correlation_id is None
    assert len(decision.inputs) == 1
    assert decision.inputs[0].id == "lease:LR-2026-999"
    assert decision.action == "fabric.object.updated"
    assert decision.intent.startswith("Direct write to")
    assert projection.store.count() == 1
