# ee/pocketpaw_ee/terrarium/scheduler.py
# Created: 2026-09-07 (feat/terrarium-launch-runtime) — the terrarium CLOCK.
#
# A single process-local sweeper (the ``mandates/scheduler.py`` shape): every
# interval it asks the service which live universes are due a tick from their
# OWN physics and fires ``service.tick(..., n=1)`` for each. A world day is
# ``time.world_day_seconds`` long and gets ``time.ticks_per_day`` ticks; when
# nobody has read the Journal for a world day the universe is DORMANT and gets
# ``time.dormant_ticks_per_day`` instead — slower, not dead.
#
# Gated by POCKETPAW_CLOUD_SCHEDULER_ENABLED (pytest never spawns a loop);
# interval from POCKETPAW_TERRARIUM_SCHEDULER_INTERVAL (seconds, default 60).
# ``due_state`` is pure so the arithmetic is testable with a frozen clock; the
# doc reads stay in service.py (the sole importer of the Beanie docs).

"""The terrarium clock: one sweep per interval, one tick per due universe."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_GATE_ENV = "POCKETPAW_CLOUD_SCHEDULER_ENABLED"
_INTERVAL_ENV = "POCKETPAW_TERRARIUM_SCHEDULER_INTERVAL"
_DEFAULT_INTERVAL_SECONDS = 60
_TASK: asyncio.Task | None = None

TriggerFn = Callable[[str, str, str, int], Awaitable[dict[str, Any]]]
NowFn = Callable[[], datetime]


def _aware(dt: datetime | None) -> datetime | None:
    # mongomock hands back naive datetimes; treat them as UTC.
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def due_state(
    time: dict[str, Any],
    *,
    now: datetime,
    last_tick_at: datetime | None,
    last_viewed_at: datetime | None,
) -> tuple[bool, str]:
    """(tick_is_due, status) from the physics ``time`` block and the two stamps.

    Dormant when the last view is older than one world day (never viewed counts
    as dormant). Due when the last tick is older than one tick-interval at the
    current cadence (never ticked counts as due)."""
    day = max(1, int(time.get("world_day_seconds", 3600)))
    tpd = max(1, int(time.get("ticks_per_day", 12)))
    dpd = max(1, int(time.get("dormant_ticks_per_day", 1)))
    now = _aware(now) or now
    viewed, ticked = _aware(last_viewed_at), _aware(last_tick_at)
    dormant = viewed is None or (now - viewed).total_seconds() > day
    per_tick = day / (dpd if dormant else tpd)
    due = ticked is None or (now - ticked).total_seconds() >= per_tick
    return due, ("dormant" if dormant else "running")


async def run_scheduler_tick(
    *, now: NowFn | None = None, trigger: TriggerFn | None = None
) -> list[str]:
    """ONE sweep: tick every due universe once. Returns the ids that ticked.
    Never raises — a failing universe is logged and the sweep moves on."""
    from pocketpaw_ee.terrarium import service

    clock: NowFn = now or (lambda: datetime.now(UTC))
    fire: TriggerFn = trigger or service.tick
    try:
        due = await service.scheduler_sweep(clock())
    except Exception:  # noqa: BLE001
        logger.warning("terrarium clock: due-list read failed — skipping", exc_info=True)
        return []
    ticked: list[str] = []
    for row in due:
        try:
            await fire(row["workspace_id"], row["user_id"], row["universe_id"], 1)
            ticked.append(row["universe_id"])
        except Exception:  # noqa: BLE001 — one universe never blocks the next
            logger.warning("terrarium clock: tick failed for %s", row["universe_id"], exc_info=True)
    return ticked


def _interval_seconds() -> int:
    raw = os.environ.get(_INTERVAL_ENV, "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_INTERVAL_SECONDS
    except ValueError:
        return _DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else _DEFAULT_INTERVAL_SECONDS


async def _scheduler_loop(interval: int) -> None:
    logger.info("terrarium clock: loop started (interval=%ds)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        try:
            await run_scheduler_tick()
        except Exception:  # noqa: BLE001
            logger.warning("terrarium clock: sweep failed", exc_info=True)


async def start_scheduler(*, interval_seconds: int | None = None) -> None:
    global _TASK
    await stop_scheduler()
    interval = interval_seconds if interval_seconds is not None else _interval_seconds()
    _TASK = asyncio.create_task(_scheduler_loop(interval), name="terrarium-clock")


async def stop_scheduler() -> None:
    global _TASK
    task, _TASK = _TASK, None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def is_running() -> bool:
    return _TASK is not None and not _TASK.done()


def gate_enabled() -> bool:
    return os.environ.get(_GATE_ENV, "").lower() == "true"


async def reconcile_scheduler(*, interval_seconds: int | None = None) -> int:
    """Lifespan startup: start the loop iff the gate env is true and no loop is
    live. Returns 1 when it started one, else 0. Never raises."""
    if not gate_enabled() or is_running():
        return 0
    try:
        await start_scheduler(interval_seconds=interval_seconds)
    except Exception:  # noqa: BLE001
        logger.warning("terrarium clock: failed to start", exc_info=True)
        return 0
    return 1


async def shutdown_scheduler() -> None:
    await stop_scheduler()


__all__ = [
    "due_state",
    "is_running",
    "reconcile_scheduler",
    "run_scheduler_tick",
    "shutdown_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
