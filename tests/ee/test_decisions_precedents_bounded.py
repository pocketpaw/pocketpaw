# tests/ee/test_decisions_precedents_bounded.py — Pins the cost and the
# result of the projection's precedent fallback (RFC 07 A3 path 2).
#
# `DecisionProjection._fallback_precedents` runs once per completed
# decision, so a journal replay calls it n times. It must hydrate a
# bounded number of rows (the top 3 siblings), not every decision in the
# pocket, or `rebuild` goes O(n^2). The golden test pins that the bounded
# query returns exactly what the original full-scan-and-filter version
# returned: same ids, same order (ts DESC, id DESC), same weights,
# including ts ties and same-ts rows with a different action.
#
# Every store here lives on `tmp_path`; nothing touches ~/.soul.

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pocketpaw_ee.cloud.decisions.domain import Decision, DecisionRef
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.store import DecisionStore
from soul_protocol.spec.journal import Actor

_POCKET = "p_bulk"
_ACTIONS = ["send_to_tenant", "approve_invoice", "close_ticket", "escalate", "archive"]
_BASE_TS = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _reference_fallback_precedents(store: DecisionStore, decision: Decision) -> list[DecisionRef]:
    """The pre-fix implementation, verbatim apart from `self._store` →
    `store`. Kept as the behavioural oracle for the bounded query."""
    if not decision.pocket_id:
        return []
    siblings = [
        d
        for d in store.iter_decisions(
            pocket_id=decision.pocket_id,
        )
        if d.id != decision.id and d.action == decision.action and d.ts < decision.ts
    ]
    out: list[DecisionRef] = []
    for i, sib in enumerate(siblings[:3]):
        weight = round(0.95 - (i * 0.1), 2)
        out.append(DecisionRef(decision_id=sib.id, relation="precedent", weight=weight))
    return out


def _decision(*, ts: datetime, action: str, pocket_id: str | None = _POCKET) -> Decision:
    return Decision(
        id=uuid4(),
        ts=ts,
        decided_by=Actor(kind="agent", id="did:soul:bulk", scope_context=["org:nerve"]),
        scope=["org:nerve", f"pocket:{pocket_id}"],
        intent="bulk",
        action=action,
        pocket_id=pocket_id,
    )


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[DecisionStore, list[Decision]]:
    """~2,000 decisions in one pocket: mixed actions, mostly distinct ts,
    every 7th row shares the previous row's ts (ties, sometimes with a
    different action), some rows carry microseconds. Plus a second pocket
    so the pocket filter is exercised."""
    rng = random.Random(1234)
    store = DecisionStore(tmp_path / "decisions.db")
    rows: list[Decision] = []
    ts = _BASE_TS
    for i in range(2000):
        if i % 7 != 0:
            ts = ts + timedelta(seconds=1, microseconds=rng.choice([0, 0, 250_000, 1]))
        rows.append(_decision(ts=ts, action=rng.choice(_ACTIONS)))
    for i in range(50):
        rows.append(
            _decision(ts=_BASE_TS + timedelta(seconds=i), action=_ACTIONS[0], pocket_id="p_other")
        )
    for d in rows:
        store.upsert_decision(d)
    yield store, rows
    store.close()


def test_fallback_precedents_hydrates_a_bounded_number_of_rows(
    seeded: tuple[DecisionStore, list[Decision]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, rows = seeded
    projection = DecisionProjection(store=store)
    target = rows[1990]

    calls = 0
    real = store._hydrate_decision

    def counting(row):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real(row)

    monkeypatch.setattr(store, "_hydrate_decision", counting)

    result = projection._fallback_precedents(target)

    assert len(result) == 3
    assert calls <= 10, f"hydrated {calls} rows for one precedent lookup"


def test_fallback_precedents_matches_reference(
    seeded: tuple[DecisionStore, list[Decision]],
) -> None:
    store, rows = seeded
    projection = DecisionProjection(store=store)
    rng = random.Random(99)

    samples = rng.sample(rows[:2000], 40)
    # Tie rows and their same-ts neighbours (often a different action).
    samples += [rows[i] for i in (7, 8, 6, 700, 701, 1400, 1995)]
    # Not-yet-stored decisions, the real call path: same ts as a stored
    # row, every action, plus an empty-pocket and an other-pocket case.
    for action in _ACTIONS:
        samples.append(_decision(ts=rows[1400].ts, action=action))
    samples.append(_decision(ts=rows[0].ts, action=rows[0].action))  # nothing earlier
    samples.append(
        _decision(ts=rows[1999].ts + timedelta(days=1), action=_ACTIONS[2], pocket_id=None)
    )
    samples.append(
        _decision(ts=_BASE_TS + timedelta(seconds=30), action=_ACTIONS[0], pocket_id="p_other")
    )
    assert len(samples) >= 50

    non_empty = 0
    for d in samples:
        got = projection._fallback_precedents(d)
        want = _reference_fallback_precedents(store, d)
        assert [(r.decision_id, r.relation, r.weight) for r in got] == [
            (r.decision_id, r.relation, r.weight) for r in want
        ], f"mismatch for decision at {d.ts.isoformat()} action={d.action}"
        non_empty += bool(want)
    assert non_empty > 30  # the oracle is actually exercising the ranking


def test_reference_sees_ties_ordered_by_id_desc(
    seeded: tuple[DecisionStore, list[Decision]],
) -> None:
    """Guard that the fixture really produces same-ts rows the ranking has
    to break by id, so the golden test covers the tie-breaker."""
    store, rows = seeded
    by_ts: dict[datetime, list[UUID]] = {}
    for d in rows[:2000]:
        by_ts.setdefault(d.ts, []).append(d.id)
    assert any(len(ids) > 1 for ids in by_ts.values())
