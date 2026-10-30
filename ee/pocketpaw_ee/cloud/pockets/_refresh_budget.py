# _refresh_budget.py — cost controls for pocket data-source AUTO-refresh.
# Created: 2026-05-22 (RFC 04 M3) — RFC 04 alpha shipped read-only pocket
#   data sources refreshed on `pocket_open` / `manual`. M3 adds two
#   AUTO-refresh triggers — `interval` (a timer) and `webhook` (an inbound
#   POST). Both re-run a source with NO human in the loop, so each one is a
#   real backend call that costs money. This module is the cost gate:
#
#   1. `min_interval_seconds()` — the floor an interval is clamped UP to.
#      A binding's `refresh_interval_seconds: 1` is never honored; the
#      interval scheduler reads this and clamps.
#   2. `consume_auto_refresh(pocket_id)` — a PER-POCKET rolling-hour rate
#      limiter for interval + webhook refreshes. It is DELIBERATELY a
#      separate counter from the manual `run_source` per-(pocket, user)
#      limiter in `source_executor._run_log`: a manual click and an
#      interval/webhook storm must not share a budget, or a flood of one
#      would starve the other. When the budget is spent the refresh is
#      SKIPPED (and logged) — never queued — so cost stays bounded.
#
# Both settings are env-driven (`POCKETPAW_SOURCE_REFRESH_*`), read through
# `pocketpaw.config.Settings` so they live in the one documented config
# home. Reading them per-call (not at import) keeps the values
# monkeypatchable in tests and live-reconfigurable.
#
# IMPORT-LINTER: pure — no Beanie, no models. Only `Settings` + stdlib.

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Hard lower bound on the configurable floor. Even an operator who sets
# POCKETPAW_SOURCE_REFRESH_MIN_INTERVAL_SECONDS=0 cannot drive the interval
# scheduler below this — a 5s floor still bounds the worst case.
_ABSOLUTE_MIN_INTERVAL_S = 5

_RATE_WINDOW_S = 3600.0  # the per-pocket budget is a rolling hour


def _settings():
    """Load PocketPaw settings. Isolated so a load failure degrades to
    defaults rather than breaking a refresh."""
    from pocketpaw.config import Settings

    return Settings.load()


def min_interval_seconds() -> int:
    """Return the interval floor — the smallest gap the interval scheduler
    will ever leave between two automatic refreshes of one source.

    A source binding's ``refresh_interval_seconds`` is clamped UP to this.
    Reads ``POCKETPAW_SOURCE_REFRESH_MIN_INTERVAL_SECONDS`` (default 60),
    itself floored by ``_ABSOLUTE_MIN_INTERVAL_S`` so a misconfigured 0
    cannot spin the loop.
    """
    try:
        configured = int(_settings().source_refresh_min_interval_seconds)
    except Exception:  # noqa: BLE001 — a bad config must not break refresh
        logger.warning("refresh-budget: min-interval config read failed", exc_info=True)
        configured = 60
    return max(_ABSOLUTE_MIN_INTERVAL_S, configured)


def clamp_interval(requested: int | None) -> int:
    """Clamp a source's requested interval to the configured floor.

    ``None`` (no interval authored) → the floor itself. A value below the
    floor → the floor. A value at or above the floor → unchanged. This is
    the ONE place a sub-floor interval is corrected — the scheduler calls
    it for every interval source on every pass.
    """
    floor = min_interval_seconds()
    if requested is None:
        return floor
    return max(floor, int(requested))


def max_per_hour() -> int:
    """Return the per-pocket auto-refresh budget — the cap on interval +
    webhook refreshes for one pocket in a rolling hour.

    Reads ``POCKETPAW_SOURCE_REFRESH_MAX_PER_HOUR`` (default 60). A value
    of 0 disables auto-refresh entirely (every interval/webhook refresh is
    skipped) — a valid, explicit "turn it off" setting.
    """
    try:
        return max(0, int(_settings().source_refresh_max_per_hour))
    except Exception:  # noqa: BLE001
        logger.warning("refresh-budget: max-per-hour config read failed", exc_info=True)
        return 60


# Per-pocket auto-refresh timestamps (rolling hour). Keyed on pocket id
# ONLY — interval and webhook refreshes share this budget so neither can
# run up unbounded cost, but it is fully separate from the manual
# `source_executor._run_log` per-(pocket, user) limiter.
_auto_refresh_log: dict[str, list[float]] = {}

# Guards the check-and-record on ``_auto_refresh_log`` — the read-filter-
# write is a TOCTOU race when an interval pass and a webhook hit overlap.
_auto_refresh_lock = asyncio.Lock()


async def consume_auto_refresh(pocket_id: str) -> bool:
    """Try to spend one unit of ``pocket_id``'s auto-refresh budget.

    Returns ``True`` when the refresh is PERMITTED (and records the spend),
    ``False`` when the budget is exhausted (caller skips the refresh and
    logs it — never queues). The check-and-record runs under a lock so an
    interval pass and a concurrent webhook hit cannot both race past the
    cap.
    """
    budget = max_per_hour()
    now = time.monotonic()
    window_start = now - _RATE_WINDOW_S
    async with _auto_refresh_lock:
        stamps = [t for t in _auto_refresh_log.get(pocket_id, []) if t >= window_start]
        if len(stamps) >= budget:
            _auto_refresh_log[pocket_id] = stamps
            return False
        stamps.append(now)
        _auto_refresh_log[pocket_id] = stamps
        return True


def reset_budget() -> None:
    """Clear the auto-refresh log. Test-only — production never calls it."""
    _auto_refresh_log.clear()


__all__ = [
    "clamp_interval",
    "consume_auto_refresh",
    "max_per_hour",
    "min_interval_seconds",
    "reset_budget",
]
