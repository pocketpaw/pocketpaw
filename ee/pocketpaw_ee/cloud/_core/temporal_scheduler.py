# ee/pocketpaw_ee/cloud/_core/temporal_scheduler.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — in-process
# cron driver for the RFC 03 v2 temporal trigger sweeper. Periodically
# sweeps every pocket that carries at least one ``type: temporal``
# trigger, detects rising edges (false → true per-row predicate), and
# dispatches the trigger's action via the Wave 3a gate when an edge
# fires.
#
# Design (mirrors ``cycles/scheduler.py`` and
# ``pockets/refresh_scheduler.py``):
#
#   - One loop, one task. Every ``_INTERVAL_SECONDS`` it scans the
#     workspace × pocket cross-product, picks pockets that declare at
#     least one temporal trigger, and calls
#     ``temporal_dispatcher.sweep_pocket`` for each.
#   - Per-pocket failures are logged + swallowed so one bad pocket
#     can't kill the loop for everyone else.
#   - Cancel-safe: the loop re-raises ``CancelledError``;
#     ``stop_scheduler`` cancels and awaits it so cloud teardown is
#     clean.
#   - Opt-in via ``POCKETPAW_TEMPORAL_SWEEP_ENABLED=true`` (default OFF
#     so pytest runs and multi-replica deploys don't spawn a background
#     loop). Mirrors the cycles snapshot-scheduler gate.
#   - Cadence is env-configurable via
#     ``POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS`` (default 3600 = 1h
#     per RFC "typically hourly"). Floor at 60s to prevent runaway
#     loops a misconfigured tiny value would cause.
#
# Out of scope (per the architect brief):
#   * HA / leader election. Single-node only for v0. A multi-replica
#     deploy must either gate this scheduler to one replica via the
#     env flag or accept duplicate sweeps (state writes are
#     idempotent; HTTP dispatches are not).
#   * Per-tenant cadence overrides — env var only, one cadence for the
#     whole deployment.
#   * Cron syntax for non-1h intervals. Env var is a simple integer
#     second count.
#   * Manual "sweep now" route. Future PR.
#   * (Closed 2026-05-28, Wave 3e) Per-pocket template resolution now
#     goes through ``pockets.service.resolve_pocket_template`` — the
#     scheduler still skips pockets without a resolvable template, but
#     pockets that DO carry a ``template_slug`` get a real
#     ``PocketTemplate`` here.
#
# Hard constraint: this module imports the ``temporal_dispatcher`` +
# ``pockets.service`` (for the workspace + pocket scan). It MUST NOT
# import a Beanie document class directly — the scan helper is in
# ``pockets.service`` which is the only Beanie writer for pockets.

"""In-process temporal trigger sweep scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Default cadence: 1h per RFC 03 v2 "typically hourly". Configurable
# via the env var.
_DEFAULT_INTERVAL_SECONDS = 3600
# Floor: 60s — anything tighter risks runaway loops on a
# misconfigured deployment. Mirrors ``_refresh_budget.clamp_interval``.
_MIN_INTERVAL_SECONDS = 60

_ENV_FLAG = "POCKETPAW_TEMPORAL_SWEEP_ENABLED"
_ENV_INTERVAL = "POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS"

# Module-level handle on the running task. The cloud lifecycle hook
# has no FastAPI ``app`` to stash it on (LifecycleHook takes no args),
# so the task lives here. ``start_scheduler`` / ``stop_scheduler`` are
# idempotent against this single slot — mirrors the refresh-scheduler
# pattern.
_task: asyncio.Task | None = None


def is_enabled() -> bool:
    """True when the temporal scheduler is switched on for this process."""
    return os.environ.get(_ENV_FLAG, "").lower() == "true"


def _interval_seconds() -> int:
    """Resolve the per-tick cadence, honoring the env var.

    Defaults to ``_DEFAULT_INTERVAL_SECONDS`` (1h). A configured value
    below ``_MIN_INTERVAL_SECONDS`` is clamped UP — a hallucinated
    ``1`` cannot wedge the loop. Non-integer values fall back to the
    default.
    """
    raw = os.environ.get(_ENV_INTERVAL, "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "temporal-scheduler: %s=%r is not an integer, falling back to %ds",
            _ENV_INTERVAL,
            raw,
            _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS
    if value < _MIN_INTERVAL_SECONDS:
        logger.warning(
            "temporal-scheduler: %s=%d below floor — clamping to %ds",
            _ENV_INTERVAL,
            value,
            _MIN_INTERVAL_SECONDS,
        )
        return _MIN_INTERVAL_SECONDS
    return value


async def _resolve_pocket_template_and_rows(
    workspace_id: str,
    pocket_id: str,
) -> tuple[object | None, list[dict]]:
    """Return ``(template, rows)`` for one pocket, or ``(None, [])``.

    Wave 3e — wired through ``pockets.service.resolve_pocket_template``.
    A pocket with no ``template_slug``, an unknown slug, or a stale
    on-disk template still returns ``(None, [])`` so the scheduler's
    skip-cheaply behaviour for unresolvable pockets is unchanged. A
    pocket that DOES carry a resolvable slug returns
    ``(template, [])`` — ``rows`` stays empty in v0 (the OSS sweeper
    is row-driven; a future PR will wire a materialized row source
    like the data-sources cache or Fabric).

    Returning ``(None, [])`` short-circuits the dispatcher's early-
    return path: it does no row work, persists no state, and emits
    no completion event.
    """
    # Lazy import — keep the scheduler's static import graph minimal
    # for the import-linter contract (this module is Beanie-pure).
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    template = await pockets_service.resolve_pocket_template(workspace_id, pocket_id)
    return template, []


async def run_one_pass() -> int:
    """Run a single scan-and-sweep pass across every pocket.

    Returns the number of pockets visited. Extracted from the loop
    so a test can exercise one pass without the interval sleep.
    Per-pocket failures are caught here so one bad pocket cannot
    abort the pass.

    Scan strategy: iterate every Pocket that has a non-null
    ``rippleSpec`` (the universe of pockets that might carry
    triggers). Per pocket, resolve the template; when no template
    resolves, skip cheaply. When a template does resolve and carries
    a temporal trigger, call ``sweep_pocket``.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    started = datetime.now(UTC)
    try:
        # v0: re-use the existing ``list_interval_source_pockets`` scan
        # as a stand-in. It returns every pocket with a non-null
        # ``rippleSpec.sources``. A pocket with temporal triggers but
        # no sources is missed by this scan in v0 — documented
        # limitation. The follow-up resolver PR will replace this with
        # a proper "list pockets with temporal triggers" scan helper.
        pockets = await pockets_service.list_interval_source_pockets()
    except Exception:
        logger.exception("temporal-scheduler: failed to list pockets")
        return 0

    visited = 0
    for pocket in pockets:
        pocket_id = pocket.get("pocket_id")
        workspace_id = pocket.get("workspace_id")
        if not pocket_id or not workspace_id:
            continue
        try:
            template, rows = await _resolve_pocket_template_and_rows(workspace_id, pocket_id)
            if template is None:
                # No template resolved — skip cheaply, do not write
                # state. Logged at DEBUG so a 1000-pocket deployment
                # doesn't flood logs every hour.
                logger.debug(
                    "temporal-scheduler: pocket=%s no template resolved — skipping",
                    pocket_id,
                )
                continue
            await temporal_dispatcher.sweep_pocket(
                workspace_id,
                pocket_id,
                template=template,
                rows=rows,
            )
            visited += 1
        except Exception:
            logger.exception(
                "temporal-scheduler: sweep failed for pocket=%s",
                pocket_id,
            )

    duration = (datetime.now(UTC) - started).total_seconds()
    logger.info(
        "temporal-scheduler: pass complete — pockets visited=%d duration=%.2fs",
        visited,
        duration,
    )
    return visited


async def _loop() -> None:
    """The scheduler loop body — forever, one pass per interval.

    Sleeps interval-seconds between passes. The very first pass also
    waits one interval (no immediate sweep at boot) so a deployment
    that flips the flag mid-startup isn't surprised by an
    instantaneous sweep before the rest of the cloud has finished
    coming up.
    """
    interval = _interval_seconds()
    logger.info("temporal-scheduler: loop started (interval=%ds)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("temporal-scheduler: loop cancelled — exiting")
            raise
        # ``run_one_pass`` already swallows per-pocket errors; the
        # outer guard defends against an unexpected failure in the
        # scan helper itself so the loop never dies.
        try:
            await run_one_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("temporal-scheduler: unexpected pass failure")


async def start_scheduler() -> None:
    """Start the temporal sweep loop. Idempotent and gated.

    A no-op when ``POCKETPAW_TEMPORAL_SWEEP_ENABLED`` is not ``true``
    or when a loop is already running. Called from the cloud
    ``CloudLifecycleHook.on_startup`` hook in
    ``ee/pocketpaw_ee/extensions.py``.
    """
    global _task
    if not is_enabled():
        logger.debug("temporal-scheduler: disabled (%s not 'true')", _ENV_FLAG)
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="pocket-temporal-sweep")


async def stop_scheduler() -> None:
    """Cancel + await the temporal sweep loop. Safe to call repeatedly."""
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
    """Clear the module-level task handle. Test-only."""
    global _task
    _task = None


__all__ = [
    "is_enabled",
    "run_one_pass",
    "start_scheduler",
    "stop_scheduler",
]
