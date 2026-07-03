# ee/cloud/admin_proposals/executor.py — apply an approved workspace-admin action.
# Created: 2026-07-03 (feat/workspace-admin-tools, WA-2).
#
# What this module does (the apply-on-approve half of the admin-action gate): the
# propose helper (``admin_proposals.propose.propose_admin_action``) files an
# Instinct Action carrying an ``_admin_action`` blob THROUGH Instinct (the human
# approve/reject layer). After a human approves the Action, the ee instinct
# router's ``approve_action`` fires ``execute_approved_admin_action`` here —
# exactly mirroring how ``external_actions.executor.execute_approved_external_action``
# fires for an external connector call. This function:
#
#   1. Reads the ``_admin_action`` blob from ``action.parameters``. A missing blob
#      → return (no chain was opened). A schema-mismatched blob → mark_failed +
#      close the chain, return.
#   2. WHITELIST dispatch — the blob's RBAC ``action`` key must resolve to an entry
#      in ``_DISPATCH`` (the whitelist). An unknown / absent key → HARD FAIL
#      (mark_failed + close); a write is NEVER fired for an unrecognised action.
#   3. Re-validates the args hash off the persisted blob — an args edit between
#      propose and approve → mark_failed + close, no call fires (a human approved a
#      SPECIFIC write).
#   4. Idempotency guard — if the blob's outcome is already recorded, or the Action
#      is already terminal, the write is NOT re-fired.
#   5. RE-CHECKS RBAC AT EXECUTE TIME (the load-bearing security rule): loads the
#      PROPOSER's User doc FRESH and calls ``check_workspace_action(proposer,
#      workspace_id, action)`` against their CURRENT workspace role. If the
#      proposer was demoted since proposing (or removed from the workspace), the
#      approved action FAILS CLOSED (mark_failed + close) — the whitelisted service
#      is NEVER called. This is what stops a since-revoked admin's approved write.
#   6. Calls the whitelisted service via its arg adapter and records the outcome the
#      same way the external-action executor does, closing the chain exactly once.
#
# EXACTLY-ONE-TERMINAL discipline (critical): on APPROVE the EXECUTOR owns the
# ``decision.completed`` chain close — the router does NOT emit it (mirrors the
# external-action executor). On REJECT the ROUTER owns the close and the executor
# never runs. Every terminal path here goes through the single ``_fail`` chokepoint
# (failure) or the one success emit at the end — never both.
#
# Never raises — a failure here must not break the approve response. The router
# wraps the call too; this is belt-and-braces. A service error / RBAC re-check
# denial / whitelist miss is captured as ``status=failed`` with a ``failed``
# terminal, NOT re-raised.
#
# Security (this code performs a workspace-admin WRITE on approval):
#   * WHITELIST-only — only actions seeded in ``_DISPATCH`` can ever fire; an
#     unknown action key hard-fails.
#   * RBAC is RE-CHECKED at execute time against the proposer's CURRENT role — a
#     demoted / removed proposer's approved action fails closed.
#   * the args hash is re-validated — a tampered blob is refused, not fired.
#   * tenancy: the write is scoped by the blob's ``workspace_id``; an empty
#     workspace_id is refused. The router's ``_assert_admin_action_workspace`` is
#     the primary gate; this is belt-and-braces.

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.admin_proposals.propose import (
    ADMIN_ACTION_PARAM_KEY,
    ADMIN_ACTION_SCHEMA,
    compute_args_hash,
)

logger = logging.getLogger(__name__)

# Max length of the outcome summary persisted onto the blob.
_OUTCOME_SUMMARY_MAX = 500


@dataclass(frozen=True)
class _Dispatch:
    """One whitelist entry: the service callable to invoke + an arg adapter that
    maps the blob's ``args`` dict to the callable's kwargs.

    ``service`` is an async callable. ``adapt`` receives ``(args, workspace_id,
    proposer_user_id)`` and returns the kwargs dict passed to ``service``. An
    adapter that raises ``KeyError`` / ``ValueError`` on missing/invalid args
    surfaces as a MalformedArgs failure (caught by the caller) — the write never
    fires with half-resolved args.
    """

    service: Callable[..., Awaitable[Any]]
    adapt: Callable[[dict[str, Any], str, str], dict[str, Any]]


def _adapt_member_role_change(
    args: dict[str, Any], workspace_id: str, proposer_user_id: str
) -> dict[str, Any]:
    """Adapt an ``args`` blob to ``workspace.service.update_member_role`` kwargs.

    Required args: ``target_user_id`` + ``role``. The actor is the PROPOSER (the
    one whose CURRENT role was just re-checked), so the audit row records who
    authored the change — not the approving operator.
    """
    target_user_id = str(args.get("target_user_id") or "")
    role = str(args.get("role") or "")
    if not target_user_id or not role:
        raise ValueError("member.role_change requires target_user_id and role")
    return {
        "workspace_id": workspace_id,
        "target_user_id": target_user_id,
        "role": role,
        "actor_user_id": proposer_user_id,
    }


async def _call_update_member_role(**kwargs: Any) -> Any:
    """Lazy-import wrapper so the whitelist table doesn't import the workspace
    service at module load (matches the router's lazy-import discipline)."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    return await workspace_service.update_member_role(
        kwargs["workspace_id"],
        kwargs["target_user_id"],
        kwargs["role"],
        kwargs["actor_user_id"],
    )


# THE WHITELIST — the ONLY workspace-admin actions an approved ``_admin_action``
# can ever fire. Seeded with member.role_change; extend deliberately, never
# dynamically. An action key absent from this table hard-fails at execute time.
_DISPATCH: dict[str, _Dispatch] = {
    "workspace.member.role_change": _Dispatch(
        service=_call_update_member_role,
        adapt=_adapt_member_role_change,
    ),
}


def _coerce_uuid(raw: Any) -> UUID | None:
    """Coerce a value to a ``UUID``, or ``None`` if it can't be."""
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


def _summarize(text: str) -> str:
    """Bound + single-line an outcome summary."""
    text = str(text).replace("\n", " ").strip()
    if len(text) > _OUTCOME_SUMMARY_MAX:
        text = text[:_OUTCOME_SUMMARY_MAX] + "…"
    return text


async def _persist_outcome(
    *,
    store: Any,
    action_id: str,
    status: str,
    response_summary: str,
    executed_at: str,
) -> None:
    """Back-write the outcome onto the persisted ``_admin_action`` blob.

    Direct SQL update — the same pattern the external-action executor's
    ``_persist_outcome`` uses. Best-effort: a write failure leaves the blob
    without the structured outcome but the free-text ``mark_executed`` /
    ``mark_failed`` outcome still records it. The structured outcome is ALSO the
    idempotency signal ``_already_executed`` reads.
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(ADMIN_ACTION_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["outcome"] = {
            "status": status,
            "response_summary": response_summary,
            "executed_at": executed_at,
        }
        params[ADMIN_ACTION_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — back-write is best-effort
        logger.warning(
            "admin_action: failed to persist outcome onto action %s — the "
            "structured outcome is unavailable (free-text outcome still recorded)",
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
    """Emit the ``decision.completed`` chain-close for an admin action.

    Mirrors the external-action executor's ``_emit_chain_close`` — the executor
    owns the chain close on the approve path. Returns early when
    ``correlation_id`` is None. Best-effort: a Decision-Graph wiring failure must
    never break the approve response.
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
            "admin_action decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


def _already_executed(action: Any, blob: dict[str, Any]) -> bool:
    """Idempotency guard — True when this admin write already fired.

    Either signal is sufficient: the blob already carries an ``outcome`` (a prior
    run back-wrote it), or the Action is already terminal (executed / failed).
    Re-invocation (bulk re-approve, retry) must never double-fire the write.
    """
    if isinstance(blob.get("outcome"), dict):
        return True
    status = getattr(action, "status", None)
    status_value = getattr(status, "value", status)
    return str(status_value) in ("executed", "failed")


async def _recheck_rbac(workspace_id: str, proposer_user_id: str, rbac_action: str) -> None:
    """RE-CHECK the proposer's CURRENT RBAC role at execute time. FAIL-CLOSED.

    THE load-bearing security rule of WA-2: an admin action approved in the Tray
    must fire ONLY if the PROPOSER still holds the required role RIGHT NOW. If the
    proposer was demoted (or removed from the workspace) between proposing and the
    approval landing, the approved action must NOT execute.

    Loads the proposer's User doc FRESH (so ``.workspaces`` reflects the current
    role) and runs ``check_workspace_action`` — the same helper the tool ran at
    propose time — which resolves the CURRENT role and raises ``Forbidden`` if it's
    below the action's minimum. A missing proposer (removed from the workspace, or
    a bad id) is ``resolve_workspace_role`` → ``Forbidden`` too. Raises ``Forbidden``
    on any deny; the caller turns that into a ``failed`` outcome (never executes).
    """
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User as _UserDoc
    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden

    try:
        proposer = await _UserDoc.get(PydanticObjectId(proposer_user_id))
    except Exception as exc:  # noqa: BLE001 — malformed id / DB error → fail closed
        raise Forbidden(
            "admin_action.proposer_unresolved",
            f"could not resolve the proposer for the execute-time RBAC re-check: {exc}",
        ) from exc
    if proposer is None:
        raise Forbidden(
            "admin_action.proposer_unresolved",
            "the proposer no longer exists — refusing the approved admin write",
        )
    # Raises Forbidden if the proposer's CURRENT role is below the action minimum
    # (e.g. demoted to MEMBER since proposing). Audits the denial via log_denial.
    check_workspace_action(proposer, workspace_id, rbac_action)


async def execute_approved_admin_action(
    action: Any,
    *,
    human_event_id: Any | None = None,
) -> None:
    """Execute the workspace-admin write carried by a freshly-approved Action.

    Called best-effort from the instinct router's ``approve_action`` /
    ``bulk_approve_actions`` after ``store.approve()`` succeeds — the same hook
    shape the external-action executor uses.

    ``human_event_id`` is the id of the ``human.corrected`` event the router
    emitted just before calling this — threaded through so the terminal
    ``decision.completed`` chains its ``causation_id`` back to the approval.
    ``None`` is tolerated.

    Never raises. The Action is marked executed on a successful service call, or
    failed (with a clear outcome) on ANY of: whitelist miss, schema/args mismatch,
    execute-time RBAC denial, or service error. The Decision-Graph chain is closed
    exactly once.
    """
    from pocketpaw.stores import get_instinct_store

    params = getattr(action, "parameters", None) or {}
    blob = params.get(ADMIN_ACTION_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not an admin-action Action at all — no chain was opened; nothing to
        # close, and no workspace to scope a store to.
        logger.warning(
            "approved action %s carries no _admin_action blob", getattr(action, "id", "?")
        )
        return

    correlation_id = _coerce_uuid(blob.get("correlation_id"))
    workspace_id = str(blob.get("workspace_id") or "")
    # HTTP approve path (no ``current_workspace`` ContextVar) — scope the store to
    # the blob's workspace so the terminal audit row + chain-close land in the
    # tenant's file, not the shared ledger.
    store = get_instinct_store(workspace_id=workspace_id or None)
    proposer_user_id = str(blob.get("proposer_user_id") or "")
    # The chain actor on the terminal is the proposer (the write's author).
    causation = _coerce_uuid(human_event_id)

    async def _fail(reason: str, *, error_class: str, response_summary: str | None = None) -> None:
        """Mark the Action failed AND close the chain with one terminal — the
        single failure-path chokepoint so a path can never both fail and
        double-fire the terminal."""
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
            user_id=proposer_user_id,
            causation_id=causation,
            response_summary=_summarize(response_summary or reason),
        )

    # Schema guard — a stale blob from an incompatible build fails loud rather
    # than firing a misinterpreted admin write.
    if blob.get("schema") != ADMIN_ACTION_SCHEMA:
        await _fail(
            "admin-action schema mismatch — the blob is from an incompatible "
            "build and cannot be executed",
            error_class="SchemaMismatch",
        )
        return

    # Tenancy — an admin action with no workspace is unexecutable.
    if not workspace_id:
        await _fail(
            "admin-action blob carries no workspace_id — cannot scope the write",
            error_class="MissingWorkspace",
        )
        return

    if not proposer_user_id:
        await _fail(
            "admin-action blob carries no proposer_user_id — cannot re-check RBAC",
            error_class="MissingProposer",
        )
        return

    rbac_action = str(blob.get("action") or "")
    call_args = blob.get("args")
    if not rbac_action or not isinstance(call_args, dict):
        await _fail(
            "admin-action blob is missing the action key or args",
            error_class="MalformedBlob",
        )
        return

    # WHITELIST — an action key NOT in the dispatch table NEVER executes. This is
    # the hard fail for an unknown/absent action: the executor is not a generic
    # RPC surface; only seeded workspace-admin actions can fire.
    dispatch = _DISPATCH.get(rbac_action)
    if dispatch is None:
        await _fail(
            f"admin-action '{rbac_action}' is not in the execute whitelist — refusing to run it",
            error_class="ActionNotWhitelisted",
        )
        return

    # Hash guard — a human approved a SPECIFIC write. If the args were edited
    # between propose and approve the recomputed hash won't match; refuse.
    stored_hash = str(blob.get("params_hash") or "")
    recomputed = compute_args_hash(rbac_action, call_args)
    if stored_hash and stored_hash != recomputed:
        await _fail(
            "admin-action args hash mismatch — the write args changed after the "
            "proposal was approved; refusing to fire a different write",
            error_class="ArgsHashMismatch",
        )
        return

    # Idempotency — never double-fire the write on a re-invocation.
    if _already_executed(action, blob):
        logger.info(
            "admin_action: action %s already executed (idempotency guard) — "
            "skipping the service call",
            action.id,
        )
        return

    # EXECUTE-TIME RBAC RE-CHECK (the load-bearing security rule). The proposer's
    # CURRENT role is resolved fresh; a demoted / removed proposer fails closed
    # here BEFORE any service call. A Forbidden → failed outcome, NOT an exception.
    try:
        await _recheck_rbac(workspace_id, proposer_user_id, rbac_action)
    except Exception as exc:  # noqa: BLE001 — Forbidden (deny) or a resolve error → fail closed
        await _fail(
            f"execute-time RBAC re-check failed for proposer {proposer_user_id} on "
            f"'{rbac_action}' — the proposer no longer holds the required role; "
            f"refusing the approved admin write: {exc}",
            error_class=type(exc).__name__,
            response_summary=str(exc),
        )
        return

    # Adapt the args to the whitelisted service's kwargs. A missing/invalid arg
    # surfaces as a MalformedArgs failure — the write never fires half-resolved.
    try:
        kwargs = dispatch.adapt(call_args, workspace_id, proposer_user_id)
    except Exception as exc:  # noqa: BLE001 — never let a bad adapter break approve
        await _fail(
            f"admin-action args could not be adapted for '{rbac_action}': {exc}",
            error_class="MalformedArgs",
            response_summary=str(exc),
        )
        return

    # Fire the whitelisted service. Any failure is captured as a failed outcome —
    # NEVER re-raised into the router.
    try:
        result = await dispatch.service(**kwargs)
    except Exception as exc:  # noqa: BLE001 — never let a service crash break approve
        logger.warning(
            "admin_action: service call failed for action %s (action=%s)",
            action.id,
            rbac_action,
            exc_info=True,
        )
        await _fail(
            f"admin action '{rbac_action}' failed: {exc}",
            error_class=type(exc).__name__,
            response_summary=str(exc),
        )
        return

    response_summary = _summarize("ok" if result is None else repr(result))

    # Success — mark executed, back-write the structured outcome, close the chain.
    await store.mark_executed(
        action.id,
        f"admin action '{rbac_action}' executed: {response_summary}",
    )
    await _persist_outcome(
        store=store,
        action_id=str(action.id),
        status="executed",
        response_summary=response_summary,
        executed_at=datetime.now(UTC).isoformat(),
    )
    # The ONLY terminal on the happy path (every failure path above closed via
    # ``_fail`` and returned), so exactly one ``decision.completed`` lands.
    _emit_chain_close(
        passed=True,
        action_outcome="landed",
        error_class=None,
        reason=None,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        user_id=proposer_user_id,
        causation_id=causation,
        response_summary=response_summary,
    )
    logger.info(
        "admin_action: executed action %s → '%s' (ok)",
        action.id,
        rbac_action,
    )


__all__ = ["execute_approved_admin_action"]
