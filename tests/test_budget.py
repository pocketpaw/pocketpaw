"""Unit tests for budget window/snapshot logic."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from pocketpaw.budget import (
    clear_expired_budget_override,
    get_budget_snapshot,
    get_budget_window,
    sync_budget_state,
)


class DummyTracker:
    """Minimal UsageTracker-like stub used for budget tests."""

    def __init__(self, total_cost_usd: float) -> None:
        self.total_cost_usd = total_cost_usd

    def get_summary(self, since: str | None = None) -> dict[str, float]:
        _ = since
        return {"total_cost_usd": self.total_cost_usd}


def _settings(**overrides):
    base = {
        "budget_monthly_usd": 10.0,
        "budget_warning_threshold": 0.8,
        "budget_auto_pause": True,
        "budget_reset_day": 1,
        "budget_paused": False,
        "budget_override_usd": None,
        "budget_override_reason": "",
        "budget_override_expires_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_budget_window_previous_month_when_before_reset_day() -> None:
    now = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
    window = get_budget_window(reset_day=5, now=now)

    assert window.start.isoformat() == "2026-03-05T00:00:00+00:00"
    assert window.end.isoformat() == "2026-04-05T00:00:00+00:00"


def test_budget_window_current_month_when_after_reset_day() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    window = get_budget_window(reset_day=5, now=now)

    assert window.start.isoformat() == "2026-04-05T00:00:00+00:00"
    assert window.end.isoformat() == "2026-05-05T00:00:00+00:00"


def test_clear_expired_override() -> None:
    settings = _settings(
        budget_override_usd=25.0,
        budget_override_reason="temp",
        budget_override_expires_at="2026-04-01T00:00:00+00:00",
    )

    changed = clear_expired_budget_override(settings, now=datetime(2026, 4, 2, tzinfo=UTC))

    assert changed is True
    assert settings.budget_override_usd is None
    assert settings.budget_override_reason == ""
    assert settings.budget_override_expires_at is None


def test_budget_snapshot_levels() -> None:
    settings = _settings(budget_monthly_usd=10.0, budget_warning_threshold=0.8, budget_reset_day=1)

    ok = get_budget_snapshot(
        settings,
        tracker=DummyTracker(3.0),
        now=datetime(2026, 4, 10, tzinfo=UTC),
    )
    warning = get_budget_snapshot(
        settings,
        tracker=DummyTracker(8.2),
        now=datetime(2026, 4, 10, tzinfo=UTC),
    )
    exhausted = get_budget_snapshot(
        settings,
        tracker=DummyTracker(10.0),
        now=datetime(2026, 4, 10, tzinfo=UTC),
    )

    assert ok.level == "ok"
    assert warning.level == "warning"
    assert exhausted.level == "exhausted"
    assert exhausted.exhausted is True


def test_sync_budget_state_auto_pause_on_exhaustion() -> None:
    settings = _settings(budget_auto_pause=True, budget_paused=False)

    snapshot, changed = sync_budget_state(
        settings,
        tracker=DummyTracker(11.0),
        now=datetime(2026, 4, 10, tzinfo=UTC),
    )

    assert snapshot.exhausted is True
    assert changed is True
    assert settings.budget_paused is True
