# tests/ee/test_action_sweeper.py
# Created: 2026-05-26 (RFC 09 Slice 4 — feat/rfc-09-slice-4-reconciler)
#
# Pins the abandon-path sweeper contract:
#
#   1. TTL-based fire — Actions older than ``ttl_days`` get swept; newer
#      Actions don't.
#   2. Correct chain close payload — emits
#      ``decision.completed(passed=False, action_outcome="abandoned",
#      reason="parked_ttl_expired_<N>d")`` with causation_id chained
#      back to the parked ``policy.evaluated``.
#   3. Idempotency — sweeping the same Action twice doesn't double-emit
#      (the Action is flipped to ``expired`` so the next sweep skips it).
#   4. No-sweep when nothing is over TTL — returns 0, no journal writes.
#   5. Action state flip — swept Actions are marked ``expired`` so the
#      UI no longer offers approve/reject buttons.
#   6. Non-pocket-write Actions are flipped expired but skip the chain
#      emit (no chain to close).

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite
import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.decisions._action_sweeper import (  # noqa: E402
    sweep_abandoned_actions,
)
from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    journal_dep.reset_journal_cache()
    original = journal_dep._cached_journal

    def _stub() -> object:
        return j

    journal_dep._cached_journal = _stub  # type: ignore[assignment]
    yield j
    journal_dep._cached_journal = original  # type: ignore[assignment]
    journal_dep.reset_journal_cache()
    j.close()


@pytest.fixture
def graph(tmp_path: Path) -> DecisionGraph:
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


@pytest.fixture
def instinct_store(tmp_path: Path) -> InstinctStore:
    """Fresh Instinct SQLite store. Pull it via the ee.api accessor
    pattern the sweeper uses — patch ``get_instinct_store`` so the
    sweeper resolves OUR store, not the global singleton."""
    return InstinctStore(tmp_path / "instinct.db")


@pytest.fixture(autouse=True)
def patch_instinct_store(monkeypatch, instinct_store: InstinctStore):
    """Patch the sweeper's lazy ``ee.api.get_instinct_store`` lookup so
    the test's fixture wins."""
    monkeypatch.setattr("pocketpaw_ee.api.get_instinct_store", lambda: instinct_store)


async def _seed_pending_action(
    store: InstinctStore,
    *,
    pocket_id: str,
    workspace_id: str,
    correlation_id: UUID | None,
    parked_policy_event_id: str | None = None,
    created_at: datetime | None = None,
    parameters_override: dict | None = None,
) -> str:
    """Seed a pending pocket-write Action with the requested age.

    The store's ``propose`` always stamps ``created_at = NOW``, so we
    drop in via raw SQL when the test wants a stale row.
    """
    if parameters_override is not None:
        params = parameters_override
    else:
        params = {
            "_pocket_write": {
                "schema": 2,
                "action": "mark_renewed",
                "method": "POST",
                "path": f"/{pocket_id}/renew",
                "params": {},
                "idempotency_key": None,
                "outcome": None,
                "workspace_id": workspace_id,
                "requested_by": "system-test",
                "correlation_id": str(correlation_id) if correlation_id else None,
                "parked_policy_event_id": parked_policy_event_id,
            }
        }
    if created_at is None:
        action = await store.propose(
            pocket_id=pocket_id,
            title=f"seed {pocket_id}",
            description="",
            recommendation="",
            trigger=ActionTrigger(type="agent", source="test", reason="seed"),
            category=ActionCategory.WORKFLOW,
            priority=ActionPriority.MEDIUM,
            parameters=params,
        )
        return action.id

    # Direct SQL insert so we control created_at.
    await store._ensure_schema()  # noqa: SLF001
    action_id = uuid4().hex
    async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001
        await db.execute(
            "INSERT INTO instinct_actions"
            " (id, pocket_id, title, description, category, status,"
            " priority, trigger, recommendation, parameters, context,"
            " created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                pocket_id,
                f"seed {pocket_id}",
                "",
                "workflow",
                "pending",
                "medium",
                ActionTrigger(type="agent", source="test", reason="seed").model_dump_json(),
                "",
                json.dumps(params),
                "{}",
                created_at.isoformat(),
            ),
        )
        await db.commit()
    return action_id


async def _action_status(store: InstinctStore, action_id: str) -> str:
    async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001
        cur = await db.execute("SELECT status FROM instinct_actions WHERE id = ?", (action_id,))
        row = await cur.fetchone()
        return str(row[0]) if row else ""


async def test_ttl_fires_on_older_than_30d_action(
    journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """An Action 31d old gets swept; the journal carries the close emit
    and the Action is flipped to ``expired``."""
    corr = uuid4()
    parked_policy_event_id = str(uuid4())
    old_ts = datetime.now(UTC) - timedelta(days=31)
    action_id = await _seed_pending_action(
        instinct_store,
        pocket_id="pocket-A",
        workspace_id="ws-A",
        correlation_id=corr,
        parked_policy_event_id=parked_policy_event_id,
        created_at=old_ts,
    )

    swept = await sweep_abandoned_actions(ttl_days=30)
    assert swept == 1

    closes = [e for e in journal.replay_from(0) if e.action == "decision.completed"]
    assert len(closes) == 1
    close = closes[0]
    assert close.correlation_id == corr
    assert (close.payload or {})["passed"] is False
    assert (close.payload or {})["action_outcome"] == "abandoned"
    assert (close.payload or {})["reason"] == "parked_ttl_expired_30d"
    assert close.causation_id == UUID(parked_policy_event_id)
    assert close.actor.kind == "system"

    assert await _action_status(instinct_store, action_id) == "expired"


async def test_ttl_does_not_fire_on_younger_actions(
    journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """A fresh Action stays pending."""
    corr = uuid4()
    action_id = await _seed_pending_action(
        instinct_store,
        pocket_id="pocket-A",
        workspace_id="ws-A",
        correlation_id=corr,
    )

    swept = await sweep_abandoned_actions(ttl_days=30)
    assert swept == 0

    assert [e for e in journal.replay_from(0) if e.action == "decision.completed"] == []
    assert await _action_status(instinct_store, action_id) == "pending"


async def test_sweeper_is_idempotent(
    journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """Running the sweeper twice on the same fixture produces exactly
    one ``decision.completed`` emit — the Action's ``expired`` status
    keeps it out of the second sweep's candidate set."""
    corr = uuid4()
    old_ts = datetime.now(UTC) - timedelta(days=45)
    await _seed_pending_action(
        instinct_store,
        pocket_id="pocket-B",
        workspace_id="ws-A",
        correlation_id=corr,
        created_at=old_ts,
    )

    swept_first = await sweep_abandoned_actions(ttl_days=30)
    swept_second = await sweep_abandoned_actions(ttl_days=30)
    assert swept_first == 1
    assert swept_second == 0

    closes = [e for e in journal.replay_from(0) if e.action == "decision.completed"]
    assert len(closes) == 1


async def test_no_sweep_with_empty_store(
    journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """No parked Actions at all — sweeper returns 0 and writes nothing."""
    swept = await sweep_abandoned_actions(ttl_days=30)
    assert swept == 0
    assert list(journal.replay_from(0)) == []


async def test_action_without_pocket_write_blob_is_expired_without_emit(
    journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """A pending Action with no ``_pocket_write`` blob (e.g. an
    instinct-only proposal) gets flipped to ``expired`` so the UI stops
    offering buttons, but the sweeper does NOT emit a chain close —
    there's no Decision-Graph chain to close."""
    old_ts = datetime.now(UTC) - timedelta(days=60)
    action_id = await _seed_pending_action(
        instinct_store,
        pocket_id="pocket-C",
        workspace_id="ws-A",
        correlation_id=None,
        parameters_override={},  # no _pocket_write
        created_at=old_ts,
    )

    # Returns 0 for the swept-count because non-pocket-write rows don't
    # carry chains; the Action is still flipped though.
    swept = await sweep_abandoned_actions(ttl_days=30)
    assert swept == 0
    assert await _action_status(instinct_store, action_id) == "expired"
    assert [e for e in journal.replay_from(0) if e.action == "decision.completed"] == []


async def test_action_with_blob_but_missing_correlation_skips_chain_emit(
    journal, graph: DecisionGraph, instinct_store: InstinctStore
) -> None:
    """A parked Action whose blob has no ``correlation_id`` (defensive
    coverage for the future code-path that parks without minting one)
    is still flipped expired and counted as swept — but the chain emit
    is skipped to avoid a no-correlation_id close event."""
    old_ts = datetime.now(UTC) - timedelta(days=60)
    action_id = await _seed_pending_action(
        instinct_store,
        pocket_id="pocket-D",
        workspace_id="ws-A",
        correlation_id=None,
    )
    # Update the seeded row to be old (the helper seeds NOW when no
    # created_at is passed).
    async with aiosqlite.connect(instinct_store._db_path) as db:  # noqa: SLF001
        await db.execute(
            "UPDATE instinct_actions SET created_at = ? WHERE id = ?",
            (old_ts.isoformat(), action_id),
        )
        await db.commit()

    swept = await sweep_abandoned_actions(ttl_days=30)
    assert swept == 1
    assert await _action_status(instinct_store, action_id) == "expired"
    # No chain close — no correlation_id to thread.
    assert [e for e in journal.replay_from(0) if e.action == "decision.completed"] == []
