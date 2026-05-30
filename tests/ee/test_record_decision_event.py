# tests/ee/test_record_decision_event.py — RFC 09 Slice 1b coverage for
# `pocketpaw_ee.cloud.decisions.journal_writer.record_decision_event`.
# Created: 2026-05-25 (feat/rfc-09-slice-1-record-decision-event).
#
# These tests pin the chokepoint helper that Slices 2 + 3 will route
# every chain-forming producer through. The invariants are:
#   1. Happy path — the helper appends to the journal AND folds into the
#      projection; the resulting Decision is queryable via DecisionGraph.
#   2. Ordering — journal.append fires BEFORE projection.apply. The
#      journal write is the source of truth; the projection's fold may
#      see an already-journaled entry but never the reverse. (Verified
#      via a spy that records the order of calls.)
#   3. Failure isolation — if projection.apply raises, the journal row
#      is still written, the helper returns the entry, and a warning is
#      logged. The Slice 4 reconciler is the safety net.
#   4. Cold-start rebuild — events that landed in the journal before the
#      projection started can be folded by calling
#      `projection.rebuild(journal, since_seq=0)`. This is the
#      `init_decisions_projection`-time contract.
#
# Helper file under test: ee/pocketpaw_ee/cloud/decisions/journal_writer.py.
# Sibling tests for the vocabulary rename live in test_decision_projection.py.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pocketpaw_ee.cloud.decisions.journal_writer import (
    DECISION_CHAIN_ACTIONS,
    record_decision_event,
)
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor

import pocketpaw.journal_dep as journal_dep

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path):
    """A fresh on-disk journal per test. Patches the journal_dep cache so
    `record_decision_event` (which looks the journal up via get_journal)
    sees this instance."""
    j = open_journal(tmp_path / "journal.db")
    # Replace the cache so the helper's lazy get_journal() picks ours up.
    journal_dep.reset_journal_cache()
    journal_dep._cached_journal.cache_clear()
    # Monkeypatch the cached factory to return our instance for the test.
    original = journal_dep._cached_journal

    def _stub() -> object:
        return j

    journal_dep._cached_journal = _stub  # type: ignore[assignment]
    yield j
    journal_dep._cached_journal = original  # type: ignore[assignment]
    journal_dep.reset_journal_cache()
    j.close()


@pytest.fixture
def graph(tmp_path: Path):
    """A fresh DecisionGraph + decisions.db per test, wired in as the
    process-global singleton via reset_projection_for_tests."""
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


@pytest.fixture
def base_ts() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _agent_actor() -> Actor:
    return Actor(kind="agent", id="did:soul:agent_1", scope_context=["org:nerve"])


def _user_actor() -> Actor:
    return Actor(kind="user", id="user:prakash", scope_context=["org:nerve"])


def _system_actor() -> Actor:
    return Actor(kind="system", id="system:instinct", scope_context=["org:nerve"])


# ---------------------------------------------------------------------------
# 1. Happy path — helper journals + folds; chain emits a Decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_helper_journals_and_folds(
    journal, graph: DecisionGraph, base_ts: datetime
) -> None:
    """A canonical 3-event chain (proposed → human.corrected → completed)
    routed through `record_decision_event` lands one Decision and shows up
    via DecisionGraph.find()."""
    corr = uuid4()
    scope = ["org:nerve", "pocket:p_lease"]

    record_decision_event(
        action="agent.proposed",
        correlation_id=corr,
        actor=_agent_actor(),
        scope=scope,
        ts=base_ts,
        payload={
            "intent": "Renew LR-2026-117",
            "action": "send_renewal",
            "pocket_id": "p_lease",
        },
    )
    record_decision_event(
        action="human.corrected",
        correlation_id=corr,
        actor=_user_actor(),
        scope=scope,
        ts=base_ts + timedelta(seconds=10),
        payload={"action": "approve"},
    )
    record_decision_event(
        action="decision.completed",
        correlation_id=corr,
        actor=_agent_actor(),
        scope=scope,
        ts=base_ts + timedelta(seconds=12),
        payload={"passed": True},
    )

    # Projection state: one Decision, one approver, action from the
    # proposed payload.
    assert graph.store.count() == 1
    decisions = await graph.find()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action == "send_renewal"
    assert decision.intent.startswith("Renew LR-2026-117")
    assert decision.correlation_id == corr
    assert len(decision.approvers) == 1
    assert decision.approvers[0].actor.id == "user:prakash"

    # Journal state: three events landed in append order.
    journaled = list(journal.replay_from(0))
    assert [e.action for e in journaled] == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ]
    assert all(e.correlation_id == corr for e in journaled)


# ---------------------------------------------------------------------------
# 2. Ordering — journal.append fires BEFORE projection.apply
# ---------------------------------------------------------------------------


def test_journal_append_runs_before_projection_apply(
    journal, graph: DecisionGraph, base_ts: datetime, monkeypatch
) -> None:
    """If the helper called projection.apply before journal.append, a
    crash between the two would leave the projection ahead of the
    journal. Wrap both with spies and verify the call order."""
    call_order: list[str] = []

    real_append = journal.append

    def spy_append(entry):
        call_order.append("journal.append")
        return real_append(entry)

    monkeypatch.setattr(journal, "append", spy_append)

    real_apply = graph.projection.apply

    def spy_apply(entry):
        call_order.append("projection.apply")
        return real_apply(entry)

    monkeypatch.setattr(graph.projection, "apply", spy_apply)

    record_decision_event(
        action="agent.proposed",
        correlation_id=uuid4(),
        actor=_agent_actor(),
        scope=["org:nerve", "pocket:p_lease"],
        ts=base_ts,
        payload={"intent": "x", "action": "send", "pocket_id": "p_lease"},
    )

    assert call_order == ["journal.append", "projection.apply"]


# ---------------------------------------------------------------------------
# 3. Failure isolation — projection failures don't block journal writes
# ---------------------------------------------------------------------------


def test_projection_apply_failure_does_not_block_journal_write(
    journal, graph: DecisionGraph, base_ts: datetime, caplog, monkeypatch
) -> None:
    """Per RFC 09 § Architecture, the journal is the source of truth. A
    raised exception from `projection.apply` is logged but does not
    propagate — the helper returns the entry and the journal still has
    the row. The Slice 4 reconciler will catch the missed apply."""
    corr = uuid4()

    def boom(_entry):
        raise RuntimeError("simulated projection error")

    monkeypatch.setattr(graph.projection, "apply", boom)

    caplog.set_level(logging.WARNING, logger="pocketpaw_ee.cloud.decisions.journal_writer")

    # The call MUST succeed despite the projection blowing up.
    entry = record_decision_event(
        action="agent.proposed",
        correlation_id=corr,
        actor=_agent_actor(),
        scope=["org:nerve", "pocket:p"],
        ts=base_ts,
        payload={"intent": "x", "action": "send", "pocket_id": "p"},
    )
    assert entry.action == "agent.proposed"
    assert entry.correlation_id == corr

    # Journal row exists.
    journaled = list(journal.replay_from(0))
    assert len(journaled) == 1
    assert journaled[0].correlation_id == corr

    # A warning was logged with enough context to triage.
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warning_records, "no warning logged for failed projection.apply"
    msg = warning_records[0].getMessage()
    assert "agent.proposed" in msg
    assert str(corr) in msg


# ---------------------------------------------------------------------------
# 4. Cold-start rebuild — events in the journal pre-projection still fold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_start_rebuild_folds_pre_existing_events(
    journal, tmp_path: Path, base_ts: datetime
) -> None:
    """Write three chain events directly to the journal (simulating a
    producer running before the projection's process boots), then build
    a fresh projection and call rebuild — the chain folds and the
    Decision shows up."""
    from soul_protocol.spec.journal import EventEntry

    corr = uuid4()
    scope = ["org:nerve", "pocket:p_lease"]

    # Three events appended directly — bypass the helper to mimic a
    # producer that ran in a prior process lifetime.
    events_to_seed = [
        EventEntry(
            id=uuid4(),
            ts=base_ts,
            actor=_agent_actor(),
            action="agent.proposed",
            scope=scope,
            correlation_id=corr,
            payload={"intent": "Cold start", "action": "send", "pocket_id": "p_lease"},
        ),
        EventEntry(
            id=uuid4(),
            ts=base_ts + timedelta(seconds=1),
            actor=_user_actor(),
            action="human.corrected",
            scope=scope,
            correlation_id=corr,
            payload={"action": "approve"},
        ),
        EventEntry(
            id=uuid4(),
            ts=base_ts + timedelta(seconds=2),
            actor=_agent_actor(),
            action="decision.completed",
            scope=scope,
            correlation_id=corr,
            payload={"passed": True},
        ),
    ]
    for e in events_to_seed:
        journal.append(e)

    # Fresh projection (different db path so we don't clash with other
    # tests in the same module) — cursor=0, store empty.
    set_db_path(tmp_path / "cold_start_decisions.db")
    reset_projection_for_tests()
    try:
        graph = get_decision_graph()
        assert graph.store.count() == 0

        # Bootstrap replay — fold the journal into the projection.
        applied = graph.projection.rebuild(journal.replay_from(0), since_seq=0)
        assert applied == 3
        assert graph.store.count() == 1

        decisions = await graph.find()
        assert len(decisions) == 1
        assert decisions[0].correlation_id == corr
        assert decisions[0].action == "send"
    finally:
        reset_projection_for_tests()


# ---------------------------------------------------------------------------
# 5. Decision chain action set — vocabulary fix snapshot
# ---------------------------------------------------------------------------


def test_decision_chain_actions_contains_renamed_completed() -> None:
    """RFC 09 Q2: the chain-closing action is `decision.completed`, not
    `decision.graduated`. Lock it down so a future merge can't quietly
    swap back."""
    assert "decision.completed" in DECISION_CHAIN_ACTIONS
    assert "decision.graduated" not in DECISION_CHAIN_ACTIONS
    # Outcome attachment is NOT a chain-forming action — it mutates an
    # already-emitted Decision via the outcomes service's own path.
    assert "decision.outcome_attached" not in DECISION_CHAIN_ACTIONS
    # Fabric writes contribute but don't terminate the chain — they have
    # their own (already-shipped) co-location story in FabricJournalStore.
    assert "fabric.object.updated" not in DECISION_CHAIN_ACTIONS
    # Spine actions are all present.
    assert {
        "agent.proposed",
        "human.corrected",
        "policy.evaluated",
        "decision.completed",
    } == DECISION_CHAIN_ACTIONS


# ---------------------------------------------------------------------------
# 6. Causation chaining — helper threads causation_id through
# ---------------------------------------------------------------------------


def test_helper_preserves_causation_id(journal, graph: DecisionGraph, base_ts: datetime) -> None:
    """RFC 09 § Correlation propagation calls for `human.corrected` to
    cite the prior `policy.evaluated(passed=False)` via `causation_id`.
    The helper must round-trip that field through to the journal entry."""
    corr = uuid4()
    scope = ["org:nerve", "pocket:p"]

    policy_entry = record_decision_event(
        action="policy.evaluated",
        correlation_id=corr,
        actor=_system_actor(),
        scope=scope,
        ts=base_ts,
        payload={"policy": "approve_per_row", "passed": False, "reason": "park"},
    )

    correction_entry = record_decision_event(
        action="human.corrected",
        correlation_id=corr,
        actor=_user_actor(),
        scope=scope,
        ts=base_ts + timedelta(seconds=5),
        causation_id=policy_entry.id,
        payload={"action": "approve"},
    )

    assert correction_entry.causation_id == policy_entry.id
    # Journal entry preserves the link.
    journaled = list(journal.replay_from(0))
    assert journaled[1].action == "human.corrected"
    assert journaled[1].causation_id == policy_entry.id
