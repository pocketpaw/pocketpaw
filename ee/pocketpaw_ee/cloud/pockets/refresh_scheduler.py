# refresh_scheduler.py — in-process interval-refresh loop for pocket sources.
# Created: 2026-05-22 (RFC 04 M3) — RFC 04 alpha refreshed pocket data
#   sources on `pocket_open` / `manual` only. M3 adds `"interval"` refresh:
#   a source binding may declare it should re-run on a timer. This module
#   is that timer — a single asyncio.Task, started from the cloud
#   lifecycle hook and cancelled cleanly on shutdown.
#
# Design:
#   - One loop, one task. Every `_TICK_SECONDS` it scans the pockets that
#     carry interval sources (`pockets.service.list_interval_source_pockets`)
#     and re-runs each source that is DUE.
#   - "Due" is per (pocket, source): `now - last_run >= clamp_interval(
#     binding.refresh_interval_seconds)`. The interval is FLOORED — a
#     hallucinated `refresh_interval_seconds: 1` is clamped up to the
#     configured minimum (`_refresh_budget.clamp_interval`), never honored.
#   - Each due source is re-run via `source_executor.run_sources` with
#     `only_source=<key>` — the SAME executor the manual run endpoint uses,
#     so the SSRF guards, timeouts and response cap all apply unchanged.
#   - Before each pocket's refresh the loop spends one unit of that
#     pocket's auto-refresh budget (`_refresh_budget.consume_auto_refresh`).
#     A pocket out of budget is SKIPPED (logged) — never queued — so an
#     interval storm cannot run up unbounded backend cost.
#   - One pocket erroring NEVER kills the loop: every per-pocket step is
#     wrapped, the exception logged and swallowed.
#   - Cancel-safe: the loop re-raises `CancelledError`; `stop_scheduler`
#     cancels and awaits it so cloud teardown is clean.
#
# Opt-in via `POCKETPAW_POCKET_REFRESH_SCHEDULER_ENABLED=true` — default OFF
# so a pytest run (or a multi-replica deploy that wants exactly one
# scheduler) does not spawn a background loop. Mirrors the cycles
# snapshot-scheduler gate.
#
# IMPORT-LINTER: never imports Beanie documents — the pocket scan goes
# through `pockets.service`, the refresh through `source_executor`.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from pocketpaw_ee.cloud.pockets import _refresh_budget, source_executor
from pocketpaw_ee.cloud.pockets import service as pockets_service

logger = logging.getLogger(__name__)

# How often the loop wakes to look for due sources. The actual refresh
# cadence of any one source is governed by its (floored) interval, not by
# this tick — the tick only needs to be at least as frequent as the floor.
_TICK_SECONDS = 30.0

# The synthetic actor recorded on interval-refresh audit entries + used as
# the `user_id` for the executor's per-(pocket, user) limiter. A reserved
# id no real user can hold, so an interval pass never eats a member's
# manual run budget. The PRIMARY auto-refresh cap is the per-pocket budget
# in `_refresh_budget`; the executor's per-user `_run_log` is a secondary
# floor that this synthetic id keeps off real users.
_SCHEDULER_ACTOR = "system:interval-refresh"

_ENV_FLAG = "POCKETPAW_POCKET_REFRESH_SCHEDULER_ENABLED"

# Module-level handle on the running task. The cloud lifecycle hook has no
# FastAPI `app` to stash it on (LifecycleHook takes no args), so the task
# lives here. `start`/`stop` are idempotent against this single slot.
_task: asyncio.Task | None = None

# Last-run timestamps, keyed (pocket_id, source_key). `time.monotonic`
# values — immune to wall-clock jumps. Bounded by pocket churn; a pocket
# that drops its interval sources simply stops being scanned and its
# stale entries are harmless (a few dict slots).
_last_run: dict[tuple[str, str], float] = {}


def is_enabled() -> bool:
    """True when the interval scheduler is switched on for this process."""
    return os.environ.get(_ENV_FLAG, "").lower() == "true"


def _interval_sources(sources: dict) -> dict[str, dict]:
    """Pick the interval-refresh sources out of a pocket's ``sources`` dict.

    A source qualifies when it is a dict and ``"interval"`` is in its
    ``refresh`` list. Malformed entries are skipped — the executor's own
    parse layer reports them; the scheduler just ignores them.
    """
    out: dict[str, dict] = {}
    for key, binding in (sources or {}).items():
        if not isinstance(binding, dict):
            continue
        if "interval" in (binding.get("refresh") or []):
            out[key] = binding
    return out


def _due_now(pocket_id: str, source_key: str, binding: dict, now: float) -> bool:
    """Return True when ``(pocket_id, source_key)`` is due for a refresh.

    Due when it has never run, or when ``now - last_run`` has reached the
    source's CLAMPED interval. ``clamp_interval`` floors a sub-minimum (or
    absent) ``refresh_interval_seconds`` to the configured minimum, so a
    hallucinated tiny interval cannot make a source due every tick.
    """
    interval = _refresh_budget.clamp_interval(binding.get("refresh_interval_seconds"))
    last = _last_run.get((pocket_id, source_key))
    if last is None:
        return True
    return (now - last) >= interval


async def _refresh_one_pocket(pocket: dict) -> None:
    """Re-run every DUE interval source on one pocket.

    Spends one unit of the pocket's auto-refresh budget per due source
    before the call — an out-of-budget pocket is skipped and logged, never
    queued. Each refresh goes through the shared ``source_executor`` so the
    SSRF guards apply. A failure here is caught by the caller and never
    propagates into the loop.
    """
    pocket_id = pocket["pocket_id"]
    workspace_id = pocket["workspace_id"]
    sources = _interval_sources(pocket.get("sources") or {})
    if not sources:
        return

    now = time.monotonic()
    due = {k: b for k, b in sources.items() if _due_now(pocket_id, k, b, now)}
    if not due:
        return

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    if creds is None:
        # No backend configured — the source cannot run. Stamp last_run so
        # the loop does not re-evaluate this pocket every single tick.
        for key in due:
            _last_run[(pocket_id, key)] = now
        logger.debug("refresh-scheduler: pocket %s has interval sources but no backend", pocket_id)
        return
    base_url, auth_type, auth_header, token, _allowed, _route = creds
    ripple_spec = await pockets_service.get_pocket_ripple_spec(workspace_id, pocket_id)
    if ripple_spec is None:
        return

    for key in due:
        # Always advance last_run so a persistently failing / budget-blocked
        # source backs off to its interval rather than retrying every tick.
        _last_run[(pocket_id, key)] = now
        if not await _refresh_budget.consume_auto_refresh(pocket_id):
            logger.info(
                "refresh-scheduler: pocket %s over auto-refresh budget — skipping source %s",
                pocket_id,
                key,
            )
            continue
        try:
            result = await source_executor.run_sources(
                pocket_id=pocket_id,
                user_id=_SCHEDULER_ACTOR,
                ripple_spec=ripple_spec,
                base_url=base_url,
                auth_type=auth_type,
                auth_header=auth_header,
                token=token,
                only_source=key,
            )
            if result.get("errors"):
                logger.debug(
                    "refresh-scheduler: pocket %s source %s ran with errors: %s",
                    pocket_id,
                    key,
                    result["errors"],
                )
        except Exception:
            # Per-source failure is contained — the loop and the other
            # sources/pockets continue.
            logger.warning(
                "refresh-scheduler: pocket %s source %s refresh failed",
                pocket_id,
                key,
                exc_info=True,
            )


async def run_one_pass() -> int:
    """Run a single scan-and-refresh pass across every interval pocket.

    Returns the number of pockets visited. Extracted from the loop so a
    test can exercise one pass without the 30s sleep. Per-pocket failures
    are caught here so one bad tenant cannot abort the pass.
    """
    try:
        pockets = await pockets_service.list_interval_source_pockets()
    except Exception:
        logger.exception("refresh-scheduler: failed to list interval-source pockets")
        return 0

    for pocket in pockets:
        try:
            await _refresh_one_pocket(pocket)
        except Exception:
            logger.exception(
                "refresh-scheduler: pass failed for pocket %s",
                pocket.get("pocket_id"),
            )
    return len(pockets)


async def _loop() -> None:
    """The scheduler loop body — forever, one pass per tick."""
    logger.info("refresh-scheduler: interval-refresh loop started (tick=%.0fs)", _TICK_SECONDS)
    while True:
        try:
            await asyncio.sleep(_TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("refresh-scheduler: loop cancelled — exiting")
            raise
        # run_one_pass already swallows per-pocket errors; the extra guard
        # here defends against an unexpected failure in the scan helper
        # itself so the loop never dies.
        try:
            await run_one_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("refresh-scheduler: unexpected pass failure")


async def start_scheduler() -> None:
    """Start the interval-refresh loop. Idempotent and gated.

    A no-op when ``POCKETPAW_POCKET_REFRESH_SCHEDULER_ENABLED`` is not
    ``true`` or when a loop is already running. Called from the cloud
    lifecycle ``on_startup`` hook.
    """
    global _task
    if not is_enabled():
        logger.debug("refresh-scheduler: disabled (%s not 'true')", _ENV_FLAG)
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="pocket-interval-refresh")


async def stop_scheduler() -> None:
    """Cancel + await the interval-refresh loop. Safe to call repeatedly."""
    global _task
    task = _task
    if task is None or task.done():
        _task = None
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    _task = None


def _reset_for_tests() -> None:
    """Clear the last-run map. Test-only."""
    _last_run.clear()


__all__ = [
    "is_enabled",
    "run_one_pass",
    "start_scheduler",
    "stop_scheduler",
]
