# ee/pocketpaw_ee/cloud/auth/guest_gates.py — where guest limits are ENFORCED.
#
# Created 2026-09-01 (feat/byok-guest-backend).
#
# The budget lives in ``guest_budget`` (counters, fail-closed); this module is
# the seams. Mirrors the billing gate split exactly (``credits/guards.py``):
#
#   * ``assert_guest_turn_allowed`` — CHECK-ONLY, for the synchronous chat HTTP
#     chokepoint (``chat/agent_router``). Raises the 402 the frontend's signup
#     prompt keys on BEFORE any DB write or stream start. Does NOT increment:
#     the executor is the single spend site, so a turn costs exactly one.
#   * ``reject_if_guest_over_limit`` — the ATOMIC spend, for the worker /
#     executor path (``run_core.execute_run``), sitting beside the jail-quota
#     and billing rejects. Emits the same terminal ``error`` frame shape those
#     use, then early-returns the run. Covers WS / group-DM / queued paths the
#     HTTP fast-reject never sees.
#   * ``assert_guest_can_create_session`` — the session-count cap at explicit
#     session-create (``sessions/service.create``, new-row branch only).
#
# Guests also have NO platform-credential fallback ("no keyless turns" — the
# captain's v1 rule): both turn seams refuse a guest whose workspace has no
# usable stored key with ``GuestKeyRequired`` instead of letting
# ``resolve_turn_credentials`` degrade to platform billing.
#
# Every helper is a NO-OP for non-guest users — one indexed read on the user
# row, then out. That read failing refuses the request (fail closed) rather
# than guessing the caller is not a guest.

from __future__ import annotations

import logging
import os
from typing import Any

from pocketpaw_ee.cloud._core.errors import GuestKeyRequired, GuestLimitError
from pocketpaw_ee.cloud.auth import guest_budget

logger = logging.getLogger(__name__)


def _stream_ttl() -> int:
    """Mirrors ``run_core._stream_ttl`` so a rejected run's frame expires on
    the same schedule as a normal run's (same as ``credits/guards.py``)."""
    return int(os.environ.get("POCKETPAW_CLOUD_RUN_STREAM_TTL", "3600"))


async def _byok_key_configured(workspace_id: str | None) -> bool:
    """Display-column check (never decrypts). The turn path re-resolves for
    real; this exists so the HTTP seam can refuse a keyless guest cleanly."""
    if not workspace_id:
        return False
    from pocketpaw_ee.cloud.byok import service as byok_service

    status = await byok_service.get_status(workspace_id)
    return bool(status.configured)


async def assert_guest_can_create_session(user_id: str) -> None:
    """Raise ``GuestLimitError("sessions")`` when a guest is at their cap."""
    guest = await guest_budget.load_guest(user_id)
    if guest is None:
        return
    cap = guest_budget.limits_for(guest).sessions
    if await guest_budget.session_count(user_id) >= cap:
        raise GuestLimitError("sessions")


async def assert_guest_turn_allowed(user_id: str, workspace_id: str | None) -> None:
    """CHECK-ONLY turn gate for the HTTP chokepoint. Raises, never increments.

    Order: key first (a keyless guest at their cap should be told about the
    key — fixing it changes nothing if we then also refuse on the cap, so the
    cap message wins only when the key is fine).
    """
    guest = await guest_budget.load_guest(user_id)
    if guest is None:
        return
    if not await _byok_key_configured(workspace_id or guest.active_workspace):
        raise GuestKeyRequired()
    cap = guest_budget.limits_for(guest).turns_per_day
    if await guest_budget.turns_used_today(user_id) >= cap:
        raise GuestLimitError("turns")


async def reject_if_guest_over_limit(
    user_id: str,
    workspace_id: str | None,
    *,
    run_id: str,
    transport: Any,
) -> bool:
    """Executor-seam gate: the SINGLE atomic turn spend. True = run rejected.

    On rejection: terminal ``error`` frame carrying the frozen contract keys
    (top-level ``code`` (+ ``kind`` for the cap)), ``mark_terminal(failed)``,
    stream TTL — the identical shape ``credits.guards.reject_if_over_billing``
    emits, each side effect best-effort. Fail-closed: an unreadable user row
    or counter refuses the run.
    """
    try:
        guest = await guest_budget.load_guest(user_id)
    except Exception:
        logger.warning("guest gate could not load user=%s; refusing run", user_id, exc_info=True)
        await _emit_reject(run_id, transport, GuestLimitError("turns"))
        return True
    if guest is None:
        return False

    if not await _byok_key_configured(workspace_id or guest.active_workspace):
        await _emit_reject(run_id, transport, GuestKeyRequired())
        return True

    cap = guest_budget.limits_for(guest).turns_per_day
    allowed, spent, cap = await guest_budget.try_spend_turn(user_id, cap)
    if allowed:
        logger.info("guest turn %d/%d for user=%s", spent, cap, user_id)
        return False
    await _emit_reject(run_id, transport, GuestLimitError("turns"))
    return True


async def _emit_reject(run_id: str, transport: Any, exc: Any) -> None:
    payload: dict[str, Any] = {"code": exc.code, "message": exc.message}
    kind = getattr(exc, "kind", None)
    if kind:
        payload["kind"] = kind
    logger.warning("run %s rejected — guest gate: %s", run_id, exc.code)
    try:
        await transport.append_event(run_id, "error", payload)
    except Exception:
        logger.debug("guest error frame append failed for %s", run_id, exc_info=True)
    try:
        from pocketpaw_ee.cloud.chat.runs import service as run_service

        await run_service.mark_terminal(run_id, status="failed", error=exc.message)
    except Exception:
        logger.exception("mark_terminal(failed) failed for guest-rejected run %s", run_id)
    try:
        await transport.set_ttl(run_id, _stream_ttl())
    except Exception:
        logger.debug("guest stream ttl set failed for %s", run_id, exc_info=True)


__all__ = [
    "assert_guest_can_create_session",
    "assert_guest_turn_allowed",
    "reject_if_guest_over_limit",
]
