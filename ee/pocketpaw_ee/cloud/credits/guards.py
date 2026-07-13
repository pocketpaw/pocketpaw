# ee/pocketpaw_ee/cloud/credits/guards.py — the shared, flag-gated run-start
# BILLING gate, factored out of run_core so every agent-run entry seam blocks an
# over-budget tenant the SAME way, with NO model call when rejected ("no model
# call = no overspend").
#
# Three helpers, one core check, so each caller emits the terminal/error response
# native to ITS transport (the callers are NOT identical):
#
#   * ``over_billing_limit(workspace)`` — the pure, reusable core. Flag-gated
#     (a no-op returning None unless ``get_settings().billing_enforced``). Runs
#     the SAME two assertions the chat chokepoint (chat/agent_router) runs, in the
#     SAME order: ``check_balance`` (wallet <= 0 -> InsufficientCredits, 402
#     credits.insufficient) FIRST, then ``check_quota`` (month-to-date spend >=
#     the effective monthly ceiling -> QuotaExceeded, 402 credits.quota_exceeded).
#     Returns the raised CloudError to REJECT, or None to PROCEED. It does NOT
#     emit / raise itself — the caller decides how to respond. An uncapped
#     (Enterprise, ceiling=None) plan is a no-op in ``check_quota`` by
#     construction; an unknown plan fails CLOSED to the Free ceiling (the
#     entitlements resolver). An empty workspace returns None (no wallet to
#     attribute — the run-transport callers validate a non-empty workspace
#     upstream; the event-bus callers can't gate a run they can't attribute).
#
#   * ``assert_within_billing(workspace)`` — for HTTP routes: RAISE the CloudError
#     (402) so ``_core.http`` maps it to the wire. Place it BEFORE any local
#     ``try/except Exception`` in the handler so the 402 isn't swallowed into a
#     500.
#
#   * ``reject_if_over_billing(workspace, *, run_id, transport, log_label)`` — for
#     the run/stream transport (run_core.execute_run): on rejection emit a clean
#     terminal ``error`` stream frame, ``mark_terminal(failed)`` the run doc, and
#     set the stream TTL, then return True so the caller early-returns BEFORE any
#     model/agent work. Returns False to proceed. Each side effect is best-effort
#     (a transient Redis/Mongo blip can't turn a clean reject into a crash),
#     mirroring the jail-quota reject.
#
# The credits package is imported LOCALLY inside the core so this module stays off
# the hot import graph; ``run_service`` is imported locally in the run-transport
# helper to keep the credits layer from importing chat at module load.
#
# Created 2026-07-08 (feat/billing-enforce-gate): universalize the run-start
# credit gate across every agent-run seam (chat was already gated; this adds the
# group/DM auto-response bridge, the /files Library agent ops, and the planner
# task executor). run_core's ``_reject_if_over_credit_quota`` now delegates here
# and thereby ALSO gains the ``check_balance`` (wallet <= 0) leg it lacked — the
# worker/executor path now rejects at an empty wallet, not only at the monthly
# ceiling.

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from pocketpaw.config import get_settings  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from pocketpaw_ee.cloud._core.errors import CloudError

logger = logging.getLogger(__name__)


def _stream_ttl() -> int:
    """Stream-doc TTL in seconds — mirrors ``run_core._stream_ttl`` so a rejected
    run's error frame expires on the same schedule as a normal run's."""
    return int(os.environ.get("POCKETPAW_CLOUD_RUN_STREAM_TTL", "3600"))


async def over_billing_limit(workspace_id: str) -> CloudError | None:
    """Return the billing rejection for ``workspace_id``, or None to proceed.

    The pure, transport-agnostic core of the run-start billing gate. A no-op
    (returns None) unless ``get_settings().billing_enforced`` is on — OSS /
    self-host deployments run no credit ledger. When enforced, it runs the two
    credit assertions in the SAME order as the chat chokepoint: ``check_balance``
    (empty wallet) first, then ``check_quota`` (monthly ceiling). The FIRST that
    trips is returned (an ``InsufficientCredits`` or ``QuotaExceeded``, both 402
    CloudErrors); if both pass it returns None.

    An uncapped plan (Enterprise, ``monthly_ceiling`` is None) never trips the
    quota leg (``check_quota`` no-ops there). An empty ``workspace_id`` returns
    None: there is no wallet to attribute, and the run-transport callers already
    validate a non-empty workspace upstream. Only the two credit CloudErrors are
    caught here — any other exception (a real infra failure) propagates, exactly
    as the pre-existing run_core gate let it.
    """
    if not get_settings().billing_enforced:
        return None
    if not workspace_id:
        return None

    from pocketpaw_ee.cloud._core.errors import InsufficientCredits, QuotaExceeded
    from pocketpaw_ee.cloud.credits import service as credits_service

    try:
        # Order mirrors chat/agent_router: the empty-wallet block is the primary
        # money guarantee; the monthly-ceiling block sits beside it.
        await credits_service.check_balance(workspace_id)
        await credits_service.check_quota(workspace_id)
    except (InsufficientCredits, QuotaExceeded) as exc:
        return exc
    return None


async def assert_within_billing(workspace_id: str) -> None:
    """Raise the billing rejection (402 CloudError) when over budget; else no-op.

    For HTTP routes: the raised ``InsufficientCredits`` / ``QuotaExceeded`` is a
    CloudError that ``_core.http`` maps to a 402 wire response. Call it BEFORE any
    local ``try/except Exception`` in the handler so the 402 isn't swallowed into
    a generic 500. Flag-gated and Enterprise-safe via ``over_billing_limit``.
    """
    exc = await over_billing_limit(workspace_id)
    if exc is not None:
        raise exc


async def reject_if_over_billing(
    workspace_id: str,
    *,
    run_id: str,
    transport: Any,
    log_label: str | None = None,
) -> bool:
    """Reject an over-budget run on the run/stream transport. Returns True if it did.

    The run-transport variant (run_core.execute_run): when ``over_billing_limit``
    trips, emit a terminal ``error`` stream frame, ``mark_terminal(failed)`` the
    run doc, and set the stream TTL — the SAME clean shape the ART-3 jail-quota
    reject uses — then return True so the caller early-returns BEFORE any model /
    agent work (the no-overspend money guarantee). Returns False to proceed.

    Every side effect is best-effort: a transient Redis / Mongo failure while
    writing the rejection must not turn a clean 402-equivalent block into an
    unhandled crash that takes the worker down.
    """
    exc = await over_billing_limit(workspace_id)
    if exc is None:
        return False

    label = log_label or run_id
    message = str(exc)
    logger.warning("run %s rejected — billing gate: %s (%s)", label, message, exc.code)
    try:
        await transport.append_event(run_id, "error", {"code": exc.code, "message": message})
    except Exception:
        logger.debug("billing error frame append failed for %s", run_id, exc_info=True)
    try:
        from pocketpaw_ee.cloud.chat.runs import service as run_service

        await run_service.mark_terminal(run_id, status="failed", error=message)
    except Exception:
        logger.exception("mark_terminal(failed) failed for over-billing run %s", run_id)
    try:
        await transport.set_ttl(run_id, _stream_ttl())
    except Exception:
        logger.debug("billing stream ttl set failed for %s", run_id, exc_info=True)
    return True


__all__ = [
    "assert_within_billing",
    "over_billing_limit",
    "reject_if_over_billing",
]
