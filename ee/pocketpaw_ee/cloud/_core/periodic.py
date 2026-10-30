# ee/pocketpaw_ee/cloud/_core/periodic.py — shared seam for cloud background loops.
#
# Today it holds one thing: ``sweep_tick``, which every cloud asyncio sweep wraps
# around ONE iteration so paw-lens sees the sweep as a monitor (kind ``sweep``,
# slug ``sweep:<name>``) and the iteration's spans carry ``paw.automation.*``.
# It is a thin alias over ``pocketpaw.lens_checkins.automation_run``; check-ins
# never raise and never block the tick.
#
# This module is where the canonicalization plan's E1 periodic-loop helper
# (interval reader with a floor, create_task, cancel-safe stop, sweep_runtime
# mark, wrapped by ``_core.lease.leased``) is meant to land. When E1 is built it
# absorbs ``sweep_tick`` into its tick call and the per-loop call sites go away.
"""Per-iteration paw-lens monitoring for cloud background sweeps."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from pocketpaw.lens_checkins import automation_run


def sweep_tick(
    name: str, interval_seconds: float | None = None, *, crontab: str | None = None
) -> AbstractAsyncContextManager[None]:
    """Wrap one sweep iteration: ``async with sweep_tick("name", interval): await tick()``."""
    # Sub-second intervals would round to 0, which is no schedule at all.
    schedule = (
        {"crontab": crontab}
        if crontab
        else {"interval_seconds": round(interval_seconds)}
        if interval_seconds and interval_seconds >= 1
        else None
    )
    return automation_run("sweep", name, schedule=schedule)
