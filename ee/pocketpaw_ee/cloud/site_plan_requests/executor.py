# ee/cloud/site_plan_requests/executor.py — an admin approved the request, so
# buy the plan and publish the site.
#
# Created: 2026-09-01 (feat/sites-plan-purchase-request).
#
# The apply-on-approve half. Called best-effort from the instinct router's
# approve / bulk-approve after ``store.approve()`` succeeds, exactly like
# ``admin_proposals.executor``. Never raises — a failure here must not break the
# approve response; it is captured as a ``failed`` outcome the Tray card shows.
#
# THE ONE INVERSION, and the reason this is its own module rather than another
# ``_DISPATCH`` row in ``admin_proposals``:
#
#   ``_admin_action`` re-checks the PROPOSER's current role at execute time,
#   because there the proposer is an admin whose rights might have been revoked
#   since. Here the proposer is a MEMBER who never had the right — that is the
#   entire premise of the feature — so re-checking them would refuse every
#   request this exists to serve. The check runs against the APPROVER instead:
#   ``Action.approved_by``, which the instinct router writes from the
#   authenticated identity (``str(user.id)``), never from a request body field.
#
# That is safe because ``instinct.approve`` is ADMIN and ``sites.buy_plan`` is
# ADMIN — an approver has necessarily cleared the purchase bar already. The check
# is re-run anyway rather than inferred: those two rules live in different files
# and can drift, and "the approve endpoint checked something similar" is not a
# control. If ``sites.buy_plan`` is ever raised to OWNER, this keeps working and
# an ADMIN's approval starts failing closed with a legible reason — which is the
# correct behaviour and the reason the check is explicit.
#
# The order of guards is load-bearing and mirrors admin_proposals: blob present →
# schema → tenancy → approver identity → identity-hash → idempotency (under a
# per-action lock, re-read fresh) → RBAC re-check → the write. Nothing that
# charges runs before all of them.
#
# PRICE DRIFT is reported, not enforced. The blob records the price the requester
# saw; the purchase prices from the live catalog, because the catalog is the
# truth about money and a week-old snapshot is not. When they differ the outcome
# says so, so an admin who approved "$7/month" can see they were charged $9. The
# alternative — refusing on any drift — turns an ordinary price change into a
# pile of dead Tray cards, and the identity hash already covers the thing that
# must not move (which workspace, which site, which tier).

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.site_plan_requests.propose import (
    SITE_PLAN_REQUEST_PARAM_KEY,
    SITE_PLAN_REQUEST_SCHEMA,
    compute_request_hash,
)

logger = logging.getLogger(__name__)

# Bound on an outcome summary, matching admin_proposals.
_OUTCOME_SUMMARY_MAX = 500

# Per-action locks so two concurrent invocations for the SAME Action cannot
# interleave between the idempotency guard and the outcome back-write. Keyed on
# the action id, so different actions never contend.
_action_locks: dict[str, asyncio.Lock] = {}


def _lock_for(action_id: str) -> asyncio.Lock:
    """Return the process-wide lock for ``action_id`` (minted on first use)."""
    lock = _action_locks.get(action_id)
    if lock is None:
        lock = asyncio.Lock()
        _action_locks[action_id] = lock
    return lock


def site_plan_request_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_site_plan_request`` blob on an Action, or ``None``."""
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get(SITE_PLAN_REQUEST_PARAM_KEY)
    return blob if isinstance(blob, dict) else None


def _coerce_uuid(raw: Any) -> UUID | None:
    """Coerce a value to a ``UUID``, or ``None`` if it can't be."""
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return UUID(raw)
        except (ValueError, AttributeError, TypeError):
            return None
    return None


def _summarize(text: str) -> str:
    """Bound + single-line an outcome summary."""
    text = str(text).replace("\n", " ").strip()
    if len(text) > _OUTCOME_SUMMARY_MAX:
        text = text[:_OUTCOME_SUMMARY_MAX] + "…"
    return text


def _approver_id(action: Any) -> str:
    """The AUTHENTICATED approver recorded on the Action.

    ``approved_by`` is written by ``store.approve(action_id, approver=...)``, and
    the instinct router passes ``str(user.id)`` — never ``req.approver``, which
    is a free-text display label a client controls. This is the identity whose
    role decides whether the purchase may happen, so reading the wrong field here
    would be the whole vulnerability.
    """
    return str(getattr(action, "approved_by", "") or "")


async def _persist_outcome(
    *,
    store: Any,
    action_id: str,
    status: str,
    response_summary: str,
    executed_at: str,
) -> None:
    """Back-write the structured outcome onto the persisted blob.

    Also the idempotency signal ``_already_executed`` reads. Best-effort: on
    failure the free-text ``mark_executed`` / ``mark_failed`` outcome still
    records what happened.
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(SITE_PLAN_REQUEST_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["outcome"] = {
            "status": status,
            "response_summary": response_summary,
            "executed_at": executed_at,
        }
        params[SITE_PLAN_REQUEST_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — back-write is best-effort
        logger.warning(
            "site_plan_request: failed to persist outcome onto action %s",
            action_id,
            exc_info=True,
        )


def _emit_chain_close(
    *,
    passed: bool,
    action_outcome: str,
    error_class: str | None,
    reason: str | None,
    correlation_id: UUID | None,
    workspace_id: str,
    user_id: str,
    causation_id: UUID | None,
    response_summary: str | None = None,
) -> None:
    """Emit the ``decision.completed`` chain-close.

    The executor owns the close on the approve path (the router owns it on
    reject), so exactly one terminal lands. Best-effort.
    """
    if correlation_id is None:
        return

    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    payload: dict[str, Any] = {"passed": passed, "action_outcome": action_outcome}
    if error_class:
        payload["error_class"] = error_class
    if reason:
        payload["reason"] = reason
    if response_summary:
        payload["response_summary"] = response_summary

    try:
        record_decision_completed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
            causation_id=causation_id,
        )
    except Exception:  # noqa: BLE001 — chain close is best-effort
        logger.warning(
            "site_plan_request decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


def _already_executed(action: Any, blob: dict[str, Any]) -> bool:
    """Idempotency guard — True when this purchase already fired.

    Either signal suffices: the blob carries an ``outcome``, or the Action is
    terminal. A re-approve must never buy the plan twice.
    """
    if isinstance(blob.get("outcome"), dict):
        return True
    status = getattr(action, "status", None)
    status_value = getattr(status, "value", status)
    return str(status_value) in ("executed", "failed")


async def _recheck_approver_may_buy(workspace_id: str, approver_user_id: str) -> None:
    """RE-CHECK the APPROVER's CURRENT right to buy a site plan. FAIL-CLOSED.

    Loads the approver's User doc FRESH (so ``.workspaces`` reflects their role
    right now) and runs ``check_workspace_action`` for ``sites.buy_plan`` — the
    same rule the publish router applies to a direct purchase. A missing user, a
    user who is not a member of this workspace, or one below ADMIN raises
    ``Forbidden``; the caller turns that into a ``failed`` outcome and the
    purchase never fires.

    The approver rather than the proposer — see the module header. This is the
    load-bearing security rule of the whole feature: without it, a pending
    request would buy the moment ANY approver touched it, and with the wrong
    identity in it, the check would refuse every legitimate request.
    """
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User as _UserDoc
    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden

    try:
        approver = await _UserDoc.get(PydanticObjectId(approver_user_id))
    except Exception as exc:  # noqa: BLE001 — a bad id is a denial, not a crash
        raise Forbidden(
            "sites.plan_purchase_forbidden",
            f"could not resolve the approving user {approver_user_id}",
        ) from exc
    if approver is None:
        raise Forbidden(
            "sites.plan_purchase_forbidden",
            f"the approving user {approver_user_id} no longer exists",
        )
    check_workspace_action(approver, workspace_id, "sites.buy_plan")


async def execute_approved_site_plan_request(
    action: Any,
    *,
    human_event_id: Any | None = None,
) -> None:
    """Buy the requested plan and publish the site, after an admin approved it.

    Never raises. The Action is marked executed on a successful publish, or
    failed (with a legible outcome) on any of: a malformed/stale blob, a missing
    or unauthorized approver, an identity-hash mismatch, or a publish error.

    The whole body runs under a per-action lock so two concurrent invocations on
    the same Action cannot interleave between the idempotency guard and the
    back-write — the loser re-reads the winner's committed outcome and no-ops.
    """
    action_id = str(getattr(action, "id", "") or "")
    if not action_id:
        await _execute_locked(action, human_event_id=human_event_id)
        return
    async with _lock_for(action_id):
        await _execute_locked(action, human_event_id=human_event_id)


async def _execute_locked(action: Any, *, human_event_id: Any | None = None) -> None:
    """The body of ``execute_approved_site_plan_request``, under the lock."""
    from pocketpaw.stores import get_instinct_store

    blob = site_plan_request_blob(action)
    if blob is None:
        # Not a plan-request Action — no chain was opened, and there is no
        # workspace to scope a store to.
        logger.warning(
            "approved action %s carries no _site_plan_request blob",
            getattr(action, "id", "?"),
        )
        return

    correlation_id = _coerce_uuid(blob.get("correlation_id"))
    workspace_id = str(blob.get("workspace_id") or "")
    pocket_id = str(blob.get("pocket_id") or "")
    site_plan_key = str(blob.get("site_plan_key") or "")
    requested_by = str(blob.get("requested_by") or "")
    approver_user_id = _approver_id(action)
    action_id = str(getattr(action, "id", "") or "")
    causation = _coerce_uuid(human_event_id)

    # HTTP approve path (no ``current_workspace`` ContextVar) — scope the store to
    # the blob's workspace so the terminal audit row lands in the tenant's file.
    store = get_instinct_store(workspace_id=workspace_id or None)

    async def _fail(reason: str, *, error_class: str, response_summary: str | None = None) -> None:
        """Mark failed AND close the chain through one terminal."""
        await store.mark_failed(action.id, reason)
        await _persist_outcome(
            store=store,
            action_id=str(action.id),
            status="failed",
            response_summary=_summarize(response_summary or reason),
            executed_at=datetime.now(UTC).isoformat(),
        )
        _emit_chain_close(
            passed=False,
            action_outcome="failed",
            error_class=error_class,
            reason=reason,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            # The chain actor is the REQUESTER — this is their request's story,
            # and the approver appears in the outcome summary.
            user_id=requested_by,
            causation_id=causation,
            response_summary=_summarize(response_summary or reason),
        )

    # Schema — a stale blob from an incompatible build fails loud rather than
    # buying a misread tier.
    if blob.get("schema") != SITE_PLAN_REQUEST_SCHEMA:
        await _fail(
            "site-plan-request schema mismatch — the blob is from an incompatible "
            "build and cannot be executed",
            error_class="SchemaMismatch",
        )
        return

    if not (workspace_id and pocket_id and site_plan_key):
        await _fail(
            "site-plan-request blob is missing workspace_id / pocket_id / site_plan_key",
            error_class="MalformedBlob",
        )
        return

    # No approver recorded means nothing proves a human with rights accepted this.
    # Refuse rather than fall back to the requester, who by construction may not buy.
    if not approver_user_id:
        await _fail(
            "site-plan-request carries no approved_by — cannot verify who authorized "
            "the purchase; refusing to charge the workspace",
            error_class="MissingApprover",
        )
        return

    # Identity hash — the admin approved a SPECIFIC site on a SPECIFIC tier. An
    # approve-with-edits that moved either must not buy something else.
    stored_hash = str(blob.get("params_hash") or "")
    recomputed = compute_request_hash(workspace_id, pocket_id, site_plan_key)
    if stored_hash and stored_hash != recomputed:
        await _fail(
            "site-plan-request identity hash mismatch — the workspace, site or tier "
            "changed after the request was approved; refusing to buy a different plan",
            error_class="RequestHashMismatch",
        )
        return

    # Idempotency — read the FRESH persisted state so a concurrent loser sees the
    # winner's committed outcome. The tamper guards above deliberately stay on the
    # passed-in blob (a re-read would mask a mutated in-flight object).
    guard_action = action
    guard_blob = blob
    if action_id:
        fresh = await store.get_action(action_id)
        if fresh is not None:
            guard_action = fresh
            fresh_blob = site_plan_request_blob(fresh)
            if fresh_blob is not None:
                guard_blob = fresh_blob
    if _already_executed(guard_action, guard_blob):
        logger.info(
            "site_plan_request: action %s already executed (idempotency guard) — "
            "not buying the plan twice",
            action.id,
        )
        return

    # EXECUTE-TIME RBAC RE-CHECK against the APPROVER (the load-bearing rule).
    try:
        await _recheck_approver_may_buy(workspace_id, approver_user_id)
    except Exception as exc:  # noqa: BLE001 — Forbidden or a resolve error → fail closed
        await _fail(
            f"the approving user {approver_user_id} does not hold 'sites.buy_plan' in "
            f"this workspace — refusing to charge for the site plan: {exc}",
            error_class=type(exc).__name__,
            response_summary=str(exc),
        )
        return

    # Price the purchase from the LIVE catalog, and report drift rather than
    # enforcing it — see the module header for why.
    from pocketpaw_ee.cloud.billing import site_plans

    tier = site_plans.site_scoped_tier(site_plan_key)
    if tier is None:
        await _fail(
            f"'{site_plan_key}' is no longer a plan a single site can be put on — "
            "the catalog changed since this was requested",
            error_class="TierNoLongerAvailable",
        )
        return
    live_price = int(tier.monthly_price_usd or 0)
    quoted_price = int(blob.get("monthly_price_usd") or 0)
    drift_note = (
        ""
        if live_price == quoted_price
        else f" (price changed since the request: ${quoted_price} → ${live_price}/month)"
    )

    # THE PURCHASE. ``purchase_authorized=True`` is earned by the RBAC re-check
    # above and by nothing else — it is the one place in the codebase outside the
    # publish router that may pass it, which is why the check sits immediately
    # before it with no branch in between.
    try:
        from pocketpaw_ee.sites import service as sites_service

        await sites_service.publish_pocket(
            workspace_id=workspace_id,
            # The REQUESTER remains the author of the publish — they built the
            # site. The approver authorized the spend, which the outcome records.
            user_id=requested_by,
            pocket_id=pocket_id,
            site_plan_key=site_plan_key,
            purchase_authorized=True,
        )
    except Exception as exc:  # noqa: BLE001 — never let a publish failure break approve
        logger.warning(
            "site_plan_request: publish failed for action %s (pocket=%s, tier=%s)",
            action.id,
            pocket_id,
            site_plan_key,
            exc_info=True,
        )
        await _fail(
            f"the site plan purchase failed: {exc}",
            error_class=type(exc).__name__,
            response_summary=str(exc),
        )
        return

    summary = _summarize(
        f"Published on the '{site_plan_key}' plan at ${live_price}/month, "
        f"approved by {approver_user_id}{drift_note}."
    )
    await store.mark_executed(action.id, summary)
    await _persist_outcome(
        store=store,
        action_id=str(action.id),
        status="executed",
        response_summary=summary,
        executed_at=datetime.now(UTC).isoformat(),
    )
    _emit_chain_close(
        passed=True,
        action_outcome="executed",
        error_class=None,
        reason=None,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        user_id=requested_by,
        causation_id=causation,
        response_summary=summary,
    )


__all__ = ["execute_approved_site_plan_request", "site_plan_request_blob"]
