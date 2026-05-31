# scheduler.py — opt-in in-process scheduler for the daily snapshot job.
# Created: 2026-05-16 — Mission Control backend completion. Gated on
#   ``POCKETPAW_CLOUD_SCHEDULER_ENABLED=true`` so test runs don't spawn a
#   background loop. Deployments that prefer external cron (Kubernetes
#   CronJob, Celery beat, OS cron) leave the flag unset and dispatch
#   ``snapshot_all_active`` from their platform scheduler — same callable,
#   same idempotency.
"""In-process daily snapshot scheduler.

For each active cycle in each active workspace, calls
``cycles_service._snapshot_cycle_daily`` once per UTC midnight via the
``snapshot_all_active`` wrapper.

The loop is a single ``asyncio.Task`` attached to the FastAPI app state
so the shutdown hook can cancel + await it cleanly. It sleeps until the
next UTC midnight, then runs one pass across every workspace that has
at least one active cycle. The pass is idempotent — a second call on the
same calendar day is a no-op.

Why in-process: lowest-friction wiring for single-instance deployments
and the dev loop. Multi-instance hosts should either pick a single
"scheduler" replica or run the snapshot job out-of-process so two
replicas don't double-fire (still idempotent per cycle, but each replica
would do its own scan).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from pocketpaw_ee.cloud.cycles import service as cycles_service
from pocketpaw_ee.cloud.cycles.snapshot_job import snapshot_all_active

logger = logging.getLogger(__name__)

_TASK_KEY = "_cycle_snapshot_scheduler_task"


def _seconds_until_next_utc_midnight() -> float:
    """Return the seconds until the next 00:00:00 UTC.

    Clamped to 1.0 second minimum so a clock skew at the boundary can't
    spin the loop. The next pass after midnight will be ~24h away again.
    """
    now = datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = (tomorrow - now).total_seconds()
    return max(1.0, delta)


async def _run_scheduler_loop() -> None:
    """The actual loop body.

    Runs forever, sleeping until the next UTC midnight between passes.
    Per-workspace exceptions are logged and swallowed so one bad tenant
    can't take down the loop for everyone else.
    """
    logger.info("cycle.scheduler: in-process loop started")
    while True:
        try:
            sleep_for = _seconds_until_next_utc_midnight()
            logger.debug(
                "cycle.scheduler: sleeping %.0f seconds until next UTC midnight", sleep_for
            )
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            logger.info("cycle.scheduler: loop cancelled — exiting")
            raise

        try:
            workspaces = await cycles_service.list_active_workspace_ids()
        except Exception:
            logger.exception("cycle.scheduler: failed to list active workspaces")
            continue

        for ws in workspaces:
            try:
                count = await snapshot_all_active(ws)
                logger.info("cycle.scheduler: workspace=%s snapshotted=%d", ws, count)
            except Exception:
                logger.exception("cycle.scheduler: pass failed for workspace=%s", ws)


async def start_in_process_scheduler(app: FastAPI) -> None:
    """Start the in-process loop and attach it to ``app.state`` so the
    shutdown hook can cancel it.

    Idempotent — calling start twice is a no-op (the second call sees the
    existing task in app state and bails).
    """
    existing = getattr(app.state, _TASK_KEY, None)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_run_scheduler_loop(), name="cycle-snapshot-scheduler")
    setattr(app.state, _TASK_KEY, task)


async def stop_in_process_scheduler(app: FastAPI) -> None:
    """Cancel + await the loop. Safe to call multiple times."""
    task = getattr(app.state, _TASK_KEY, None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    setattr(app.state, _TASK_KEY, None)


__all__ = [
    "start_in_process_scheduler",
    "stop_in_process_scheduler",
]
