# test_cost_tracker.py — CostTracker unit tests.
# Created: 2026-04-30 — Phase 2 of "Files as Knowledge" plan, Stage 2.D.
# Round-trips the JSON ledger, exercises month rollover, asserts can_spend
# semantics at the cap edge, and verifies the singleton cache rebuilds when
# the configured cap changes.
"""Tests for ``ee.cloud.embeddings.cost_tracker``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ee.cloud.embeddings.cost_tracker import (
    CostTracker,
    get_cost_tracker,
    reset_cost_tracker_for_tests,
)


def _ledger(tmp_path: Path) -> Path:
    return tmp_path / "embedding_cost.json"


def test_record_and_can_spend_under_cap(tmp_path: Path) -> None:
    t = CostTracker(monthly_cap_usd=1.0, path=_ledger(tmp_path))
    assert t.spent_this_month == 0.0
    assert t.can_spend(0.5)
    t.record(0.5)
    assert t.spent_this_month == 0.5
    # 0.5 spent + 0.5 estimate = exactly the cap; allowed (<= cap).
    assert t.can_spend(0.5)
    # 0.5 spent + 0.51 estimate = above cap; rejected.
    assert not t.can_spend(0.51)


def test_zero_cap_disables_check(tmp_path: Path) -> None:
    """A cap of 0 means "no cap" — the listener never blocks."""
    t = CostTracker(monthly_cap_usd=0, path=_ledger(tmp_path))
    assert t.can_spend(99999)


def test_negative_estimate_treated_as_zero(tmp_path: Path) -> None:
    t = CostTracker(monthly_cap_usd=1.0, path=_ledger(tmp_path))
    t.record(0.99)
    assert t.can_spend(-0.5)
    # And record() ignores non-positive cost.
    t.record(-1.0)
    assert t.spent_this_month == 0.99


def test_persistence_round_trip(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    t1 = CostTracker(monthly_cap_usd=2.0, path=path)
    t1.record(0.42)

    # Fresh tracker reading the same path keeps the running total.
    t2 = CostTracker(monthly_cap_usd=2.0, path=path)
    assert t2.spent_this_month == 0.42


def test_reset_when_persisted_month_doesnt_match(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    # Pretend a stale ledger from last year is on disk.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"month": "2000-01", "spent_usd": 99.0}))

    t = CostTracker(monthly_cap_usd=1.0, path=path)
    assert t.spent_this_month == 0.0


def test_corrupt_ledger_resets_and_does_not_crash(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")

    t = CostTracker(monthly_cap_usd=1.0, path=path)
    assert t.spent_this_month == 0.0
    t.record(0.1)
    # Recovers and persists the new state.
    raw = json.loads(path.read_text())
    assert raw["spent_usd"] == 0.1


def test_get_cost_tracker_singleton(monkeypatch, tmp_path: Path) -> None:
    """Repeated get_cost_tracker calls with the same cap return the same instance."""
    reset_cost_tracker_for_tests()
    # Redirect the default ledger to a temp file so production state isn't touched.
    monkeypatch.setattr(
        "ee.cloud.embeddings.cost_tracker._DEFAULT_PATH",
        _ledger(tmp_path),
    )

    class _S:
        embedding_monthly_cap_usd = 5.0

    a = get_cost_tracker(_S())
    b = get_cost_tracker(_S())
    assert a is b


def test_get_cost_tracker_rebuilds_when_cap_changes(monkeypatch, tmp_path: Path) -> None:
    reset_cost_tracker_for_tests()
    monkeypatch.setattr(
        "ee.cloud.embeddings.cost_tracker._DEFAULT_PATH",
        _ledger(tmp_path),
    )

    class _S:
        embedding_monthly_cap_usd = 5.0

    class _S2:
        embedding_monthly_cap_usd = 10.0

    a = get_cost_tracker(_S())
    b = get_cost_tracker(_S2())
    assert a is not b
    assert b.cap_usd == 10.0


def test_month_key_format() -> None:
    """Sanity check on the month key — UTC YYYY-MM."""
    expected = datetime.now(UTC).strftime("%Y-%m")
    t = CostTracker(monthly_cap_usd=1.0)
    assert t._current_month_key() == expected
