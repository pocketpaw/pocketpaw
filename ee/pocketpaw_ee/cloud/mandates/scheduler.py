# ee/pocketpaw_ee/cloud/mandates/scheduler.py
# Created: 2026-06-13 (feat/patrol-engine).
#
# CADENCE SCHEDULER — the piece that turns ``Charter.cadence`` from a persisted
# label into an always-on trigger. Until now shifts were MANUAL-ONLY (the
# ``service.trigger_shift`` "DEMO BAR: manual trigger only" note); a "weekly"
# cadence persisted but nothing fired it. This module closes that gap.
#
# SHAPE (mirrors ``autopilot.py``'s lifecycle, with one difference): autopilot
# runs a per-mandate task; the scheduler is a SINGLE process-local sweeper loop
# (like ``decisions._action_sweeper``) that wakes every interval, asks the
# service which ACTIVE mandates are cadence-DUE, and fires one shift per due
# mandate. One loop is enough — due-ness is computed per tick from the persisted
# charter + the last shift's timestamp, so there is no per-mandate state to hold.
#
# DETERMINISM: the unit of work is ``run_scheduler_tick(now, trigger)``. Both the
# clock (``now()``) and the shift trigger are INJECTABLE so a test can drive a
# tick with a frozen clock and a fake trigger — no wall-clock sleeps, no real
# LLM/foreman call. The live loop passes the real wall clock + the real
# ``service.trigger_shift``.
#
# RESILIENCE: the scheduler must NEVER crash the app. A due-list read failure,
# or any single mandate's ``trigger_shift`` raising, is logged and swallowed; the
# tick keeps sweeping the rest, the loop sleeps and tries again next interval.
#
# LIFESPAN: ``reconcile_scheduler`` starts the loop at startup (idempotent — a
# second call is a no-op when a loop is already live); ``shutdown_scheduler``
# cancels + awaits it at shutdown. Both are wired into
# ``cloud/__init__.mount_cloud`` next to the autopilot pair, under the SAME
# ``POCKETPAW_CLOUD_SCHEDULER_ENABLED`` gate (so pytest runs never spawn a
# background loop that outlives the test).

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Default sweep interval (seconds) — env ``POCKETPAW_MANDATE_SCHEDULER_INTERVAL``.
# Hourly by default: cadence resolution is days (weekly), so an hourly sweep is
# far finer than the smallest cadence window and keeps the boot-to-first-fire lag
# small without busy-spinning.
_DEFAULT_INTERVAL_SECONDS = 3600

# Process-local registry for the single sweeper loop (None when not running).
_TASK: asyncio.Task | None = None


# A shift trigger: async (workspace_id, user_id, mandate_id) -> dict. Matches
# ``service.trigger_shift``'s signature so the real function drops straight in.
TriggerFn = Callable[[str, str, str], Awaitable[dict[str, Any]]]
# A clock: () -> aware datetime. Injected so ticks are deterministic in tests.
NowFn = Callable[[], datetime]


def _wall_now() -> datetime:
    """The real wall clock (UTC-aware) — the live loop's default ``now``."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# One tick — the unit the loop runs (and the unit a test drives directly).
# ---------------------------------------------------------------------------


async def run_scheduler_tick(
    *,
    now: NowFn | None = None,
    trigger: TriggerFn | None = None,
) -> list[str]:
    """Run ONE scheduler sweep: ask the service which ACTIVE mandates are cadence
    -DUE at ``now()``, and fire one shift per due mandate via ``trigger``.

    Returns the list of mandate ids whose shift FIRED successfully (a mandate
    whose trigger raised is logged + skipped, NOT in the list). NEVER raises —
    the due-list read and every trigger call are wrapped so a bad mandate or a
    transient store error can't sink the sweep, the loop, or the app.

    ``now`` defaults to the wall clock; ``trigger`` defaults to the real
    ``service.trigger_shift``. Both are injected in tests for determinism."""
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    clock: NowFn = now or _wall_now
    fire: TriggerFn = trigger or mandate_service.trigger_shift

    try:
        due = await mandate_service.list_cadence_due(clock())
    except Exception:  # noqa: BLE001 — a due-list read failure must not sink the tick
        logger.warning("scheduler: due-list read failed — skipping this tick", exc_info=True)
        return []

    fired: list[str] = []
    for row in due:
        workspace_id = str(row["workspace_id"])
        mandate_id = str(row["mandate_id"])
        user_id = str(row.get("user_id") or "system:scheduler")
        try:
            await fire(workspace_id, user_id, mandate_id)
            fired.append(mandate_id)
        except Exception:  # noqa: BLE001 — one mandate's shift failure never sinks the sweep
            logger.warning(
                "scheduler: trigger_shift failed for mandate %s (workspace %s)",
                mandate_id,
                workspace_id,
                exc_info=True,
            )
    if fired:
        logger.info("scheduler: tick fired %d cadence-due shift(s)", len(fired))
    return fired


# ---------------------------------------------------------------------------
# The background loop + the single-task registry (start / stop).
# ---------------------------------------------------------------------------


def _interval_seconds() -> int:
    """Read the sweep interval from env, default 3600s. A non-int / non-positive
    value falls back to the default."""
    raw = os.environ.get("POCKETPAW_MANDATE_SCHEDULER_INTERVAL", "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "POCKETPAW_MANDATE_SCHEDULER_INTERVAL=%r is not an int — using %d",
            raw,
            _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else _DEFAULT_INTERVAL_SECONDS


async def _scheduler_loop(interval: int, *, run_immediate: bool) -> None:
    """The sweeper loop body. Optionally runs ONE tick immediately, then a tick
    every ``interval`` seconds. Per-tick failures are caught inside
    ``run_scheduler_tick`` already; the loop also guards the sleep + tick so
    nothing escapes. ``CancelledError`` propagates so shutdown can cancel-and-
    await cleanly."""
    logger.info("scheduler: loop started (interval=%ds)", interval)
    if run_immediate:
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await run_scheduler_tick()
            except Exception:  # noqa: BLE001 — already swallowed inside; belt-and-braces
                logger.warning("scheduler: immediate tick failed", exc_info=True)

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("scheduler: loop cancelled — exiting")
            raise
        try:
            await run_scheduler_tick()
        except Exception:  # noqa: BLE001 — a bad tick never sinks the loop
            logger.warning("scheduler: tick failed", exc_info=True)


async def start_scheduler(
    *, interval_seconds: int | None = None, run_immediate: bool = True
) -> None:
    """Start (or restart) the single background sweeper loop.

    Idempotent on restart: an existing live loop is cancelled first, then a fresh
    one is created. ``interval_seconds`` overrides the env default (tests pass a
    large value to keep the loop parked on its sleep). ``run_immediate=False``
    skips the first tick — a boot should not storm a tick the instant the process
    comes up (let the first interval elapse)."""
    global _TASK
    await stop_scheduler()
    interval = interval_seconds if interval_seconds is not None else _interval_seconds()
    _TASK = asyncio.create_task(
        _scheduler_loop(interval, run_immediate=run_immediate),
        name="mandate-scheduler",
    )


async def stop_scheduler() -> None:
    """Cancel + await the background sweeper loop. Safe + idempotent — a no-op
    when no loop is running."""
    global _TASK
    task, _TASK = _TASK, None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def is_running() -> bool:
    """True when the background sweeper loop is live."""
    return _TASK is not None and not _TASK.done()


# ---------------------------------------------------------------------------
# Lifespan wiring — the startup reconciler + the shutdown drain. Registered in
# ``cloud/__init__.mount_cloud`` under the same POCKETPAW_CLOUD_SCHEDULER_ENABLED
# gate the autopilot reconciler / decisions reconciler use.
# ---------------------------------------------------------------------------


async def reconcile_scheduler(*, interval_seconds: int | None = None) -> int:
    """STARTUP RECONCILER — start the single sweeper loop at lifespan startup.

    Idempotent: if a loop is already live (a double-mount, a re-entrant call),
    this is a no-op and returns 0. Otherwise it starts the loop with
    ``run_immediate=False`` (a boot never storms a tick) and returns 1. Never
    raises — a start failure logs and returns 0 so app startup is never blocked."""
    if is_running():
        return 0
    try:
        await start_scheduler(interval_seconds=interval_seconds, run_immediate=False)
    except Exception:  # noqa: BLE001 — a start failure must not block startup
        logger.warning("scheduler: startup reconcile failed to start the loop", exc_info=True)
        return 0
    logger.info("scheduler: startup reconciler started the sweeper loop")
    return 1


async def shutdown_scheduler() -> None:
    """SHUTDOWN DRAIN — cancel + await the sweeper loop at lifespan shutdown so
    the process exits without an orphaned task. Idempotent; never raises."""
    await stop_scheduler()


__all__ = [
    "NowFn",
    "TriggerFn",
    "is_running",
    "reconcile_scheduler",
    "run_scheduler_tick",
    "shutdown_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
