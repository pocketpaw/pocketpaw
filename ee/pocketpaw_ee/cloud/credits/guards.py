# ee/pocketpaw_ee/cloud/credits/guards.py: the shared run-start credit gate every
# agent-run seam calls, so an over-budget workspace is refused the same way
# everywhere and no model call happens on a refusal.
#
# * ``over_billing_limit(workspace)`` is the core. A no-op (None) unless
#   ``billing_enforced`` or ``credits_enforced`` is on. When enforced it runs
#   ``check_balance`` (wallet <= 0, 402 ``credits.insufficient``) first, then
#   ``check_quota`` (monthly ceiling reached, 402 ``credits.quota_exceeded``), and
#   returns the first CloudError that trips, or None. It never raises those two
#   itself; any other exception (a real infra failure) propagates. An empty
#   workspace id is a no-op: there is no wallet to attribute.
# * ``assert_within_billing(workspace)`` raises that error for HTTP routes, where
#   ``_core.http`` maps it to ``{"error": {"code", "message"}}``. Call it before
#   any local ``try/except Exception`` so the 402 is not swallowed into a 500.
# * ``reject_if_over_billing(workspace, ...)`` is the run/stream variant used by
#   ``run_core.execute_run``: it appends one terminal ``error`` frame
#   ``{"code", "message"}``, marks the run failed and sets the stream TTL, each
#   best-effort, then returns True so the caller stops before any agent work.
# * ``is_platform_funded_free(workspace)`` is the one definition of an unpaid
#   early-access workspace that the early-access guards share: plan ``free``,
#   ``payments_open`` off, and no applied ``top_up`` or ``subscription_grant``.
#
# ``credits_enforced`` gates only this chat credit check; ``billing_enforced``
# also turns on the seat, pocket, connector and other paid gates elsewhere.
# Credits and run-service imports are local to keep this module off the hot
# import graph.

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
    (returns None) unless ``billing_enforced`` or ``credits_enforced`` is on;
    OSS / self-host deployments run no credit ledger. When enforced, it runs the two
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
    settings = get_settings()
    if not (settings.billing_enforced or settings.credits_enforced):
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


async def is_platform_funded_free(workspace_id: str) -> bool:
    """True when ``workspace_id`` is an unpaid early-access workspace.

    All three must hold: the workspace resolves to the ``free`` plan, payments
    are closed (``payments_open`` is off), and its ledger holds no applied
    ``top_up`` or ``subscription_grant``. An unknown or missing workspace
    resolves to ``free`` (the entitlements resolver fails closed), so it counts
    as unpaid. The ledger leg cannot trip while payments are closed; it keeps
    the answer right once they open.
    """
    if get_settings().payments_open:
        return False

    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    ent = await entitlements_service.resolve_entitlements(workspace_id)
    if ent.plan != "free":
        return False
    return not await credits_service.has_paid_credits(workspace_id)


__all__ = [
    "assert_within_billing",
    "is_platform_funded_free",
    "over_billing_limit",
    "reject_if_over_billing",
]
