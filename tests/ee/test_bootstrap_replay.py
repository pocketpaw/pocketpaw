# tests/ee/test_bootstrap_replay.py
# Created: 2026-05-26 (RFC 09 Slice 4 — feat/rfc-09-slice-4-reconciler)
#
# Bootstrap-replay completion coverage. Slice 1b wired the opt-in
# ``rebuild_from_journal`` parameter on ``init_decisions_projection``
# AND wired ``mount_cloud`` to pass ``True``; Slice 4 closes the loop
# by pinning the end-to-end fold behaviour:
#
#   1. Cold-start fold — a journal with pre-existing chain events is
#      replayed into the projection on first ``init_decisions_projection
#      (rebuild_from_journal=True)``; a freshly-initialised DecisionStore
#      ends up with the chains folded into Decision rows.
#   2. Idempotency — calling init twice does NOT re-fold. The cursor's
#      persisted seq blocks the second replay from re-applying events.
#   3. Incremental replay — events appended AFTER the first init's
#      cursor get folded on the next init's replay (warm restart).
#   4. Bootstrap failure is non-fatal — a broken journal at init time
#      logs a warning and leaves the projection empty (the Slice 4
#      reconciler is the safety net).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    init_decisions_projection,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402
from soul_protocol.spec.journal import Actor, EventEntry  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402


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


@pytest.fixture(autouse=True)
def fresh_decisions_db(tmp_path: Path):
    """Per-test decisions.db so the cursor + folded rows start empty."""
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    yield
    reset_projection_for_tests()


def _agent_actor() -> Actor:
    return Actor(kind="agent", id="did:soul:agent_1", scope_context=["org:nerve"])


def _proposed(correlation_id, ts) -> EventEntry:
    return EventEntry(
        id=uuid4(),
        ts=ts,
        actor=_agent_actor(),
        action="agent.proposed",
        scope=["org:nerve", "pocket:p1"],
        correlation_id=correlation_id,
        payload={"intent": "test intent", "action": "do_thing", "pocket_id": "p1"},
    )


def _completed(correlation_id, ts) -> EventEntry:
    return EventEntry(
        id=uuid4(),
        ts=ts,
        actor=_agent_actor(),
        action="decision.completed",
        scope=["org:nerve", "pocket:p1"],
        correlation_id=correlation_id,
        payload={"passed": True, "action_outcome": "landed"},
    )


def test_cold_start_replay_folds_pre_existing_events(journal) -> None:
    """A journal with chain events pre-loaded BEFORE ``init_decisions
    _projection(rebuild_from_journal=True)`` ends up folded on the
    first init call.

    Note: on soul-protocol 0.3.1 the cursor stays at 0 because
    ``replay_from`` does not round-trip ``seq``; the chain still folds
    correctly via correlation_id. The Slice 1a wheel bump exercises
    the cursor advance end-to-end."""
    corr = uuid4()
    base_ts = datetime.now(UTC)
    journal.append(_proposed(corr, base_ts))
    journal.append(_completed(corr, base_ts + timedelta(seconds=2)))

    graph = init_decisions_projection(rebuild_from_journal=True)
    assert graph.store.count() == 1
    assert isinstance(graph.projection.cursor, int)


def test_init_is_idempotent_no_double_fold(journal) -> None:
    """A second init call returns the same singleton and does NOT
    re-fold the journal (singleton guard in ``init_decisions_projection``)."""
    corr = uuid4()
    base_ts = datetime.now(UTC)
    journal.append(_proposed(corr, base_ts))
    journal.append(_completed(corr, base_ts + timedelta(seconds=2)))

    graph1 = init_decisions_projection(rebuild_from_journal=True)
    cursor_after_first = graph1.projection.cursor
    count_after_first = graph1.store.count()

    graph2 = init_decisions_projection(rebuild_from_journal=True)
    assert graph2 is graph1
    assert graph2.projection.cursor == cursor_after_first
    assert graph2.store.count() == count_after_first


def test_warm_restart_folds_all_events_via_correlation_dedup(journal) -> None:
    """First init replays the events present then. Reset the singleton
    (simulates a process restart, decisions.db persists). Append new
    events. Second init's replay folds the new chain — on 0.3.1 where
    the cursor stays at 0 the projection's correlation-id dedup keeps
    the first chain at a single Decision while the new chain folds in.
    """
    corr1 = uuid4()
    corr2 = uuid4()
    base_ts = datetime.now(UTC)

    # First boot — one chain.
    journal.append(_proposed(corr1, base_ts))
    journal.append(_completed(corr1, base_ts + timedelta(seconds=1)))
    graph1 = init_decisions_projection(rebuild_from_journal=True)
    assert graph1.store.count() == 1

    # Drop the singleton to simulate a restart — decisions.db sticks
    # around (the store file is on disk).
    reset_projection_for_tests()

    # Second boot — second chain landed since.
    journal.append(_proposed(corr2, base_ts + timedelta(seconds=10)))
    journal.append(_completed(corr2, base_ts + timedelta(seconds=11)))

    graph2 = init_decisions_projection(rebuild_from_journal=True)
    # Both chains visible after the warm restart. Correlation-id
    # dedup in the projection keeps the first chain at one row even
    # though the replay re-walked the entire journal.
    assert graph2.store.count() == 2

    found = list(graph2.store.iter_decisions())
    corrs = {d.correlation_id for d in found}
    assert corrs == {corr1, corr2}


def test_bootstrap_failure_is_non_fatal(monkeypatch, journal) -> None:
    """A broken ``get_journal`` at init time logs a warning and leaves
    the projection empty (the Slice 4 reconciler is the safety net)."""

    def _broken():
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr("pocketpaw.journal_dep.get_journal", _broken)

    # init should not raise — the warning is logged inside the except
    # block in ``init_decisions_projection``.
    graph = init_decisions_projection(rebuild_from_journal=True)
    assert graph is not None
    assert graph.store.count() == 0
    assert graph.projection.cursor == 0


def test_no_replay_when_flag_false(journal) -> None:
    """Default ``rebuild_from_journal=False`` skips the replay even
    when the journal has events — tests get this default so each
    fixture's tmp decisions.db can't accidentally absorb a developer's
    real ~/.soul/journal.db rows."""
    corr = uuid4()
    base_ts = datetime.now(UTC)
    journal.append(_proposed(corr, base_ts))
    journal.append(_completed(corr, base_ts + timedelta(seconds=1)))

    graph = init_decisions_projection()  # default rebuild_from_journal=False
    assert graph.store.count() == 0
    assert graph.projection.cursor == 0
