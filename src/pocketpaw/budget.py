"""Budget helpers for monthly spend enforcement and temporary overrides."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pocketpaw import usage_tracker
from pocketpaw.config import Settings
from pocketpaw.usage_tracker import UsageTracker


@dataclass(frozen=True)
class BudgetWindow:
    """Represents one budget window between reset boundaries."""

    start: datetime
    end: datetime
    key: str


@dataclass(frozen=True)
class BudgetSnapshot:
    """Current budget state for enforcement, alerts, and UI."""

    window_start: str
    window_end: str
    window_key: str
    configured_cap_usd: float
    effective_cap_usd: float | None
    override_active: bool
    warning_threshold: float
    spent_usd: float
    remaining_usd: float | None
    percent_used: float
    level: str
    exhausted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "window_key": self.window_key,
            "configured_cap_usd": self.configured_cap_usd,
            "effective_cap_usd": self.effective_cap_usd,
            "override_active": self.override_active,
            "warning_threshold": self.warning_threshold,
            "spent_usd": self.spent_usd,
            "remaining_usd": self.remaining_usd,
            "percent_used": self.percent_used,
            "level": self.level,
            "exhausted": self.exhausted,
        }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_mock_placeholder(value: object) -> bool:
    """Detect unittest.mock auto-generated attribute placeholders."""
    module = getattr(value.__class__, "__module__", "") or ""
    return module.startswith("unittest.mock")


def _get_setting(settings: Settings, name: str, default: object) -> object:
    """Fetch a setting with defaults that also handle mocked Settings in tests."""
    value = getattr(settings, name, default)
    if _is_mock_placeholder(value):
        return default
    return value


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return _as_utc(parsed)


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _get_reset_day(settings: Settings) -> int:
    return max(1, min(28, _as_int(_get_setting(settings, "budget_reset_day", 1), 1)))


def get_budget_window(reset_day: int, now: datetime | None = None) -> BudgetWindow:
    """Return the active budget window for a reset day in [1, 28]."""
    current = _as_utc(now or datetime.now(UTC))

    if current.day >= reset_day:
        start_year = current.year
        start_month = current.month
    else:
        if current.month == 1:
            start_year = current.year - 1
            start_month = 12
        else:
            start_year = current.year
            start_month = current.month - 1

    start = datetime(start_year, start_month, reset_day, tzinfo=UTC)
    if start_month == 12:
        end_year = start_year + 1
        end_month = 1
    else:
        end_year = start_year
        end_month = start_month + 1
    end = datetime(end_year, end_month, reset_day, tzinfo=UTC)

    return BudgetWindow(
        start=start,
        end=end,
        key=f"{start.date().isoformat()}_{end.date().isoformat()}",
    )


def clear_expired_budget_override(settings: Settings, now: datetime | None = None) -> bool:
    """Clear override fields if they are invalid or already expired."""
    override_usd = _get_setting(settings, "budget_override_usd", None)
    override_expires_at = _get_setting(settings, "budget_override_expires_at", None)
    if override_usd is None and not override_expires_at:
        return False

    current = _as_utc(now or datetime.now(UTC))
    expires_at = _parse_iso_datetime(override_expires_at)

    if expires_at is None or expires_at <= current:
        setattr(settings, "budget_override_usd", None)
        setattr(settings, "budget_override_reason", "")
        setattr(settings, "budget_override_expires_at", None)
        return True
    return False


def get_effective_budget_cap(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> tuple[float | None, bool]:
    """Return (effective_cap, override_active)."""
    current = _as_utc(now or datetime.now(UTC))

    override_active = False
    override_cap: float | None = None
    override_usd = _get_setting(settings, "budget_override_usd", None)
    override_expires_at = _get_setting(settings, "budget_override_expires_at", None)
    monthly_cap = _as_float(_get_setting(settings, "budget_monthly_usd", 20.0), 20.0)

    if override_usd is not None:
        expires_at = _parse_iso_datetime(override_expires_at)
        if expires_at and expires_at > current:
            override_active = True
            override_cap = _as_float(override_usd, monthly_cap)

    if override_active:
        cap = override_cap
    else:
        cap = monthly_cap

    if cap is None or cap <= 0:
        return None, override_active
    return cap, override_active


def get_budget_snapshot(
    settings: Settings,
    *,
    tracker: UsageTracker | None = None,
    now: datetime | None = None,
) -> BudgetSnapshot:
    """Compute current spend state for the active budget window."""
    current = _as_utc(now or datetime.now(UTC))
    window = get_budget_window(_get_reset_day(settings), now=current)

    usage = tracker or usage_tracker.get_usage_tracker()
    summary = usage.get_summary(since=window.start.isoformat())
    spent = float(summary.get("total_cost_usd") or 0.0)

    cap, override_active = get_effective_budget_cap(settings, now=current)
    threshold = _as_float(_get_setting(settings, "budget_warning_threshold", 0.8), 0.8)
    threshold = min(max(threshold, 0.0), 1.0)
    configured_cap = _as_float(_get_setting(settings, "budget_monthly_usd", 20.0), 20.0)

    if cap is None:
        return BudgetSnapshot(
            window_start=window.start.isoformat(),
            window_end=window.end.isoformat(),
            window_key=window.key,
            configured_cap_usd=configured_cap,
            effective_cap_usd=None,
            override_active=override_active,
            warning_threshold=threshold,
            spent_usd=round(spent, 6),
            remaining_usd=None,
            percent_used=0.0,
            level="unlimited",
            exhausted=False,
        )

    exhausted = spent >= cap - 1e-9
    if exhausted:
        level = "exhausted"
    elif spent >= cap * threshold:
        level = "warning"
    else:
        level = "ok"

    return BudgetSnapshot(
        window_start=window.start.isoformat(),
        window_end=window.end.isoformat(),
        window_key=window.key,
        configured_cap_usd=configured_cap,
        effective_cap_usd=round(cap, 6),
        override_active=override_active,
        warning_threshold=threshold,
        spent_usd=round(spent, 6),
        remaining_usd=round(max(cap - spent, 0.0), 6),
        percent_used=round((spent / cap) * 100.0, 2),
        level=level,
        exhausted=exhausted,
    )


def sync_budget_state(
    settings: Settings,
    *,
    tracker: UsageTracker | None = None,
    now: datetime | None = None,
) -> tuple[BudgetSnapshot, bool]:
    """Normalize override/pause flags and return (snapshot, changed)."""
    changed = clear_expired_budget_override(settings, now=now)
    snapshot = get_budget_snapshot(settings, tracker=tracker, now=now)

    auto_pause = _as_bool(_get_setting(settings, "budget_auto_pause", True), True)
    paused = _as_bool(_get_setting(settings, "budget_paused", False), False)
    should_pause = auto_pause and snapshot.exhausted
    if should_pause and not paused:
        setattr(settings, "budget_paused", True)
        changed = True
    elif not should_pause and paused:
        setattr(settings, "budget_paused", False)
        changed = True

    return snapshot, changed


def set_budget_override_until_window_end(
    settings: Settings,
    *,
    cap_usd: float,
    reason: str = "",
    now: datetime | None = None,
) -> BudgetWindow:
    """Set a temporary override that expires at the next reset boundary."""
    window = get_budget_window(_get_reset_day(settings), now=now)
    setattr(settings, "budget_override_usd", cap_usd)
    setattr(settings, "budget_override_reason", reason.strip())
    setattr(settings, "budget_override_expires_at", window.end.isoformat())
    setattr(settings, "budget_paused", False)
    return window


def clear_budget_override(settings: Settings) -> None:
    """Clear any active override and unpause budget state."""
    setattr(settings, "budget_override_usd", None)
    setattr(settings, "budget_override_reason", "")
    setattr(settings, "budget_override_expires_at", None)
    setattr(settings, "budget_paused", False)
