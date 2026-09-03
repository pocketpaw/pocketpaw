# ee/pocketpaw_ee/cloud/llm_provisioning/run_end_trigger.py — bill a workspace's
# proxy spend when its run ends, instead of waiting for the next sweep tick.
#
# WHAT THIS IS FOR. In ``live`` mode the credit ledger only moves when the
# five-minute cutover sweep runs, so a customer's balance can be up to five
# minutes stale. That is a problem in two directions: someone watching their
# balance sees nothing happen after a run, and the run-start hard-block
# (``credits.check_balance``) can wave through a run that a not-yet-ingested
# charge would have paid for the last of.
#
# THE TIMING CONSTRAINT, measured rather than assumed. On 2026-09-03 against the
# production gateway a completion's spend row appeared in ``/spend/logs/v2``
# about 15 seconds after the response returned; it was NOT there at 6 seconds.
# LiteLLM writes spend rows from a background task after the response is sent, so
# reading immediately at run end reliably finds nothing. Hence the delay before
# the ingest, and hence this being a scheduled task rather than an inline await.
#
# WHY NOT READ THE COST OFF THE RESPONSE. The proxy returns
# ``x-litellm-response-cost`` and ``x-litellm-call-id`` on every completion, and
# billing straight from those would need no delay at all. Two things rule it out:
#
#   * ``x-litellm-call-id`` is NOT the id the spend log records. Measured the same
#     day: the header carried ``846f51b6-...`` while the row's ``request_id`` was
#     the response BODY's ``id`` (``chatcmpl-c4bf1015-...``). Billing on the header
#     id would key the ledger on something the sweep can never match, so the sweep
#     would bill the same call again under the row's real id. A double charge.
#   * The header only exists where OUR code makes the HTTP call. The agent
#     backends go through pydantic-ai and ChatLiteLLM, which do not surface
#     response headers, and that is where the spend actually is.
#
# So this triggers the SAME ingest the sweep runs, against the same rows, keyed on
# the same ``litellm:{request_id}``. It races the sweep by design and the race is
# safe: whichever gets there first debits, the other no-ops on the ledger's unique
# index. This changes WHEN a charge lands, never whether or how much.
#
# FIRE AND FORGET, deliberately. A billing read must never delay a user's response
# or fail their run — the compute is already served and the sweep is the backstop
# for anything this misses. Every failure path here logs and returns.
#
# Created 2026-09-04 (feat/bill-spend-at-run-end): new entity.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

logger = logging.getLogger(__name__)

# How long to wait after a run ends before reading the tenant's spend.
#
# Measured, not guessed: the proxy wrote the row at ~15s in the 2026-09-03 probe
# and had not written it at 6s. 20 seconds buys margin on that without making the
# balance feel stale. Too short is the expensive mistake — the read finds nothing,
# the trigger is wasted, and the charge waits for the sweep anyway.
_DEFAULT_DELAY_SECONDS = 20.0

# Background tasks are held here for the lifetime of their run. ``asyncio`` keeps
# only a weak reference to a task, so a fire-and-forget task with no strong
# reference can be garbage-collected mid-await and simply never finish — silently,
# which for a billing path means a charge that quietly never lands.
_pending: set[asyncio.Task] = set()


def _delay_seconds() -> float:
    """The post-run delay, overridable for tests and slow proxies.

    ``POCKETPAW_SPEND_TRIGGER_DELAY_SECONDS``. A bad value falls back to the
    default rather than raising — this is a billing timer, and a malformed env var
    must not take the trigger out.
    """
    raw = os.environ.get("POCKETPAW_SPEND_TRIGGER_DELAY_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_DELAY_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "run_end_trigger: POCKETPAW_SPEND_TRIGGER_DELAY_SECONDS=%r is not a number "
            "— using the %.0fs default",
            raw,
            _DEFAULT_DELAY_SECONDS,
        )
        return _DEFAULT_DELAY_SECONDS
    return value if value >= 0 else _DEFAULT_DELAY_SECONDS


def is_enabled() -> bool:
    """Whether run-end billing is on. Default ON, and only meaningful in ``live``.

    ``POCKETPAW_SPEND_TRIGGER_ENABLED=false`` turns it off, leaving the sweep as
    the sole path. The kill switch exists because this adds proxy reads
    proportional to run volume rather than to time, and an operator who finds that
    expensive needs a way to stop it that is not a deploy.
    """
    raw = os.environ.get("POCKETPAW_SPEND_TRIGGER_ENABLED", "").strip().lower()
    return raw not in {"false", "0", "no", "off"}


async def _ingest_after_delay(workspace: str, delay: float) -> None:
    """Wait for the proxy to write the row, then run the tenant's normal ingest."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # Shutdown, or the loop going away mid-wait. The sweep will bill this
        # tenant on its next tick, so there is nothing to recover here.
        raise

    try:
        from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

        result = await provisioning_service.ingest_tenant_spend(workspace)
    except Exception:
        # Never propagates. The run is finished, the customer has their answer, and
        # the five-minute sweep re-reads the same window.
        logger.exception(
            "run_end_trigger: spend ingest failed for workspace=%s — the sweep will "
            "retry on its next tick",
            workspace,
        )
        return

    if result.rows_billed:
        logger.info(
            "run_end_trigger: workspace=%s billed %d row(s) -> %d micro-credits "
            "(%.6f USD) %.0fs after the run ended",
            workspace,
            result.rows_billed,
            result.micro_debited,
            result.cost_usd,
            delay,
        )
    else:
        # Not a failure. The sweep's overlapping read may have got there first, or
        # the proxy is slower than the delay today, in which case the sweep bills it.
        logger.debug(
            "run_end_trigger: workspace=%s had no new spend rows %.0fs after the run "
            "ended (read %d row(s))",
            workspace,
            delay,
            result.rows_read,
        )


def schedule_spend_ingest(workspace: str, *, delay: float | None = None) -> asyncio.Task | None:
    """Schedule ``workspace``'s proxy spend to be billed shortly after a run ends.

    Returns the task (tests await it) or None when nothing was scheduled. NEVER
    raises: called from the run lifecycle, where an exception would fail a run that
    has already produced its answer.

    Only fires in ``live`` mode. In ``off`` and ``shadow`` the per-run meter is
    still charging, and running the proxy ingest alongside it would bill the same
    compute twice under two different idempotency keys — the double-bill boundary
    the cutover exists to keep closed.
    """
    if not workspace:
        return None
    if not is_enabled():
        return None

    try:
        from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

        if provisioning_service.spend_mode() != "live":
            return None
    except Exception:
        logger.debug("run_end_trigger: could not resolve the spend mode", exc_info=True)
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (a sync caller, a teardown). Nothing to schedule onto.
        return None

    task = loop.create_task(
        _ingest_after_delay(workspace, _delay_seconds() if delay is None else delay),
        name=f"spend-ingest:{workspace}",
    )
    # Hold a strong reference until it finishes — see ``_pending``.
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def drain_pending(timeout: float = 30.0) -> None:
    """Wait for scheduled ingests to finish. For shutdown and for tests.

    A task still sleeping out its delay when the process stops is simply dropped;
    the sweep bills that tenant on its next tick, which is what makes it safe to
    cancel these at any point.
    """
    if not _pending:
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.gather(*_pending, return_exceptions=True), timeout)


__all__ = ["drain_pending", "is_enabled", "schedule_spend_ingest"]
