# ee/cloud/external_actions/executor.py — apply an approved external-action call.
# Created: 2026-06-11 (feat/external-action-proposal).
#
# What this module does (the apply-on-approve half of the external-action gate):
# the propose helper (``external_actions.propose.propose_external_action``) files
# an Instinct Action carrying an ``_external_action`` blob THROUGH Instinct (the
# human approve/reject layer). After a human approves the Action, the ee instinct
# router's ``approve_action`` fires ``execute_approved_external_action`` here —
# exactly mirroring how ``instinct_bridge.execute_approved_write`` is fired for a
# parked pocket write and ``belt.executor.execute_approved_change`` for a code
# change. This function:
#
#   1. Reads the ``_external_action`` blob from ``action.parameters``. A missing
#      blob → return (no chain was opened, nothing to close). A schema-mismatched
#      blob → mark_failed + close the chain, return.
#   2. Re-validates the params hash off the persisted blob — a params edit
#      between propose and approve → mark_failed + close, no call fires (a human
#      approved a SPECIFIC call).
#   3. Idempotency guard — if the blob's ``idempotency_key`` was already recorded
#      as executed (the Action is already in a terminal state, or a prior run
#      back-wrote an outcome), the call is NOT re-fired. Bulk re-approve / retry
#      can never double-fire the external call.
#   4. Resolves the connector + calls the named action with the proposed params
#      through the CLOUD connector path (``connectors.service.execute``), which
#      loads the workspace's saved connector config fresh — NO secret is read off
#      the blob.
#   5. Back-writes the outcome ``{status, response_summary, executed_at}`` onto
#      the persisted blob via the direct-SQL pattern (the same one belt's
#      ``_persist_run_result`` and the pocket-write bridge's
#      ``_persist_parked_policy_event_id`` use).
#   6. Records the result on the Action (``mark_executed`` / ``mark_failed``) and
#      CLOSES the Decision-Graph chain exactly once.
#
# EXACTLY-ONE-TERMINAL discipline (critical): on APPROVE the EXECUTOR owns the
# ``decision.completed`` chain close — the router does NOT emit it (mirrors the
# pocket-write bridge + belt executor). On REJECT the ROUTER owns the close and
# the executor never runs. Get this wrong and chains double-close. Every terminal
# path here goes through the single ``_fail`` chokepoint (failure) or the one
# success emit at the end — never both.
#
# Never raises — a failure here must not break the approve response. The router
# wraps the call too; this is belt-and-braces. A connector error is captured as
# ``status=failed`` with a ``failed`` terminal, NOT re-raised.
#
# Security (this code fires an external call on approval):
#   * NO connector secret is read off the blob — the cloud connector service
#     loads the workspace's saved config fresh at execution.
#   * the params hash is re-validated — a tampered blob is refused, not fired.
#   * tenancy: the connector service is scoped by the blob's ``workspace_id``;
#     an empty workspace_id is refused (unexecutable). The router's
#     ``_assert_external_action_workspace`` is the primary gate; this is
#     belt-and-braces.
#   * NO secrets in logs — only action ids, connector names, and outcome status.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.external_actions.propose import (
    EXTERNAL_ACTION_PARAM_KEY,
    EXTERNAL_ACTION_SCHEMA,
    compute_params_hash,
)

logger = logging.getLogger(__name__)

# Max length of the response summary persisted onto the blob / surfaced in the
# outcome. The full connector response is not stored — only a bounded summary so
# a large payload never bloats the Instinct DB or leaks a secret into the audit
# trail.
_RESPONSE_SUMMARY_MAX = 500


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


def _summarize_response(data: Any, error: str | None) -> str:
    """Build a short, bounded, secret-free response summary for the outcome.

    Prefers the error text on a failure; otherwise a compact repr of the data,
    truncated to ``_RESPONSE_SUMMARY_MAX``. Never includes auth headers / tokens
    — the connector service returns only the action's response body, but we still
    bound + truncate so an over-large or sensitive payload can't ride into the
    audit trail in full.
    """
    if error:
        text = str(error)
    elif data is None:
        text = "ok"
    else:
        text = repr(data)
    text = text.replace("\n", " ").strip()
    if len(text) > _RESPONSE_SUMMARY_MAX:
        text = text[:_RESPONSE_SUMMARY_MAX] + "…"
    return text


async def _persist_outcome(
    *,
    store: Any,
    action_id: str,
    status: str,
    response_summary: str,
    executed_at: str,
) -> None:
    """Back-write the call outcome onto the persisted ``_external_action`` blob.

    Direct SQL update — the same pattern belt's ``_persist_run_result`` and the
    pocket-write bridge's ``_persist_parked_policy_event_id`` use. The blob's
    ``outcome`` carries ``{status, response_summary, executed_at}`` so a reader
    (audit, a runs read model, a re-invocation idempotency check) sees the result
    structurally. Best-effort: a write failure leaves the blob without the
    structured outcome but the free-text ``mark_executed`` / ``mark_failed``
    outcome still records it.
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(EXTERNAL_ACTION_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["outcome"] = {
            "status": status,
            "response_summary": response_summary,
            "executed_at": executed_at,
        }
        params[EXTERNAL_ACTION_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — back-write is best-effort
        logger.warning(
            "external_action: failed to persist outcome onto action %s — the "
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
    """Emit the ``decision.completed`` chain-close for an external-action call.

    Mirrors ``instinct_bridge._emit_bridge_chain_close`` and belt's
    ``_emit_chain_close`` — the executor owns the chain close on the approve
    path, exactly as the pocket-write bridge / belt executor own it on theirs.
    ``correlation_id`` is read off the blob; ``causation_id`` is the
    ``human.corrected`` event the router emitted just before approval so the
    terminal chains back to the human approval.

    Returns early when ``correlation_id`` is None (a blob with a malformed /
    missing id): there is no chain to close. Best-effort: a Decision-Graph wiring
    failure must never break the approve response — the journal write is the
    source of truth; the Slice 4 reconciler is the safety net.
    """
    if correlation_id is None:
        return

    # Late imports — keep the executor's import surface small and avoid a
    # circular import with the decisions package.
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    payload: dict[str, Any] = {
        "passed": passed,
        "action_outcome": action_outcome,
    }
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
            "external_action decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


async def _run_connector_call(
    *,
    workspace_id: str,
    connector_name: str,
    connector_action: str,
    params: dict[str, Any],
    scope: str,
    pocket_id: str | None,
    user_id: str,
) -> tuple[bool, Any, str | None]:
    """Resolve the connector + call the named action through the CLOUD path.

    Returns ``(success, data, error)``. Routes through
    ``connectors.service.execute`` — the canonical cloud connector surface, which
    loads the workspace's saved connector config fresh (no secret off the blob),
    dispatches by execution mode (cloud / local / sandbox), and returns an
    ``ExecuteActionResponse``. A raised exception (connector unknown, action
    unknown, local-agent unavailable, adapter crash) is caught by the caller and
    turned into a ``failed`` outcome — this helper lets it propagate so the
    caller has the exception class for the error_class on the terminal.
    """
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.connectors.dto import ExecuteActionRequest

    body = ExecuteActionRequest(
        action=connector_action,
        params=dict(params or {}),
        scope=scope if scope in ("pocket", "workspace", "user") else "workspace",
        pocket_id=pocket_id,
        user_id=user_id or None,
    )
    result = await connectors_service.execute(
        workspace_id,
        connector_name,
        body,
        user_id=user_id or None,
    )
    return bool(result.success), result.data, result.error


def _already_executed(action: Any, blob: dict[str, Any]) -> bool:
    """Idempotency guard — True when this external call already fired.

    Two signals, either is sufficient:
      * the blob already carries an ``outcome`` (a prior run back-wrote it), or
      * the Action is already in a terminal state (executed / failed).

    Re-invocation (bulk re-approve, retry) must never double-fire the HTTP call.
    The Action's ``status`` is the authoritative gate (the router only fires the
    executor on a fresh approve, but defense in depth), and the back-written
    outcome is the belt-and-braces signal in case status reads are stale.
    """
    if isinstance(blob.get("outcome"), dict):
        return True
    status = getattr(action, "status", None)
    status_value = getattr(status, "value", status)
    return str(status_value) in ("executed", "failed")


async def execute_approved_external_action(
    action: Any,
    *,
    human_event_id: Any | None = None,
) -> None:
    """Execute the external-action call carried by a freshly-approved Action.

    Called best-effort from the instinct router's ``approve_action`` /
    ``bulk_approve_actions`` after ``store.approve()`` succeeds — the same hook
    shape the pocket-write bridge and belt executor use. ``action`` is the
    approved Action.

    ``human_event_id`` is the id of the ``human.corrected`` event the router
    emitted just before calling this — threaded through so the terminal
    ``decision.completed`` event can chain its ``causation_id`` back to the
    approval, completing the causal walk ``agent.proposed → human.corrected →
    decision.completed``. ``None`` is tolerated (the chain still folds via the
    shared ``correlation_id``).

    Never raises — a failure here must not break the approve response. The router
    wraps the call too; this is belt-and-braces. The Action is marked executed on
    a successful connector call or failed with a clear outcome on any error, and
    the Decision-Graph chain is closed exactly once (success → completed/landed,
    failure → completed/failed).
    """
    from pocketpaw.stores import get_instinct_store

    params = getattr(action, "parameters", None) or {}
    blob = params.get(EXTERNAL_ACTION_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not an external-action Action at all — no chain was opened for it, so
        # there is nothing to close. We can't resolve the store's workspace
        # either (the blob carries it), so bail before opening one.
        logger.warning("approved action %s carries no _external_action blob", action.id)
        return

    # Read the chain ids off the blob up front so EVERY terminal path can close
    # the chain it opened. A malformed / missing id → None and the close no-ops
    # (the Slice 4 abandon-sweeper closes any chain left open).
    correlation_id = _coerce_uuid(blob.get("correlation_id"))
    workspace_id = str(blob.get("workspace_id") or "")
    # ISO: HTTP approve path (no ``current_workspace`` ContextVar) — scope the
    # store to the blob's workspace so the terminal audit row + chain-close land
    # in the tenant's file, not the shared ledger (and don't raise under the flag).
    store = get_instinct_store(workspace_id=workspace_id or None)
    requested_by = str(blob.get("requested_by") or "")
    approver = str(getattr(action, "approved_by", "") or "") or requested_by or "system"
    causation = _coerce_uuid(human_event_id)

    async def _fail(reason: str, *, error_class: str, response_summary: str | None = None) -> None:
        """Mark the Action failed AND close the chain with one terminal — the
        single failure-path chokepoint so a path can never both fail and
        double-fire the terminal. Best-effort back-write of the structured
        failed outcome too."""
        await store.mark_failed(action.id, reason)
        await _persist_outcome(
            store=store,
            action_id=str(action.id),
            status="failed",
            response_summary=response_summary or reason,
            executed_at=datetime.now(UTC).isoformat(),
        )
        _emit_chain_close(
            passed=False,
            action_outcome="failed",
            error_class=error_class,
            reason=reason,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=approver,
            causation_id=causation,
            response_summary=response_summary,
        )

    # Schema guard — a stale blob from an incompatible build fails loud rather
    # than firing a misinterpreted external call.
    if blob.get("schema") != EXTERNAL_ACTION_SCHEMA:
        await _fail(
            "external-action schema mismatch — the blob is from an incompatible "
            "build and cannot be executed",
            error_class="SchemaMismatch",
        )
        return

    # Tenancy — an external action with no workspace is unexecutable. The
    # connector service is scoped by workspace_id; an empty one would never
    # resolve a connector config anyway, but fail loud so a malformed blob is
    # recorded, not silently dropped.
    if not workspace_id:
        await _fail(
            "external-action blob carries no workspace_id — cannot scope the call",
            error_class="MissingWorkspace",
        )
        return

    connector_name = str(blob.get("connector_name") or "")
    connector_action = str(blob.get("action") or "")
    call_params = blob.get("params")
    if not connector_name or not connector_action or not isinstance(call_params, dict):
        await _fail(
            "external-action blob is missing connector_name, action, or params",
            error_class="MalformedBlob",
        )
        return

    # Hash guard — a human approved a SPECIFIC call. If the params were edited
    # between propose and approve, the recomputed hash won't match the stored
    # one; refuse rather than fire a different call than the one approved.
    stored_hash = str(blob.get("params_hash") or "")
    recomputed = compute_params_hash(connector_action, call_params)
    if stored_hash and stored_hash != recomputed:
        await _fail(
            "external-action params hash mismatch — the call params changed after "
            "the proposal was approved; refusing to fire a different call",
            error_class="ParamsHashMismatch",
        )
        return

    # Idempotency — never double-fire the external call on a re-invocation.
    if _already_executed(action, blob):
        logger.info(
            "external_action: action %s already executed (idempotency guard) — "
            "skipping the connector call",
            action.id,
        )
        return

    scope = str(blob.get("scope") or "workspace")
    pocket_id = blob.get("pocket_id")
    pocket_id = str(pocket_id) if pocket_id else None

    # Fire the connector call. Any failure (connector unknown, action unknown,
    # local-agent unavailable, adapter crash) is captured as a failed outcome —
    # NEVER re-raised into the router.
    try:
        success, data, error = await _run_connector_call(
            workspace_id=workspace_id,
            connector_name=connector_name,
            connector_action=connector_action,
            params=call_params,
            scope=scope,
            pocket_id=pocket_id,
            user_id=approver,
        )
    except Exception as exc:  # noqa: BLE001 — never let a connector crash break approve
        logger.warning(
            "external_action: connector call crashed for action %s (connector=%s)",
            action.id,
            connector_name,
            exc_info=True,
        )
        await _fail(
            f"connector call failed: {exc}",
            error_class=type(exc).__name__,
            response_summary=str(exc),
        )
        return

    response_summary = _summarize_response(data, error)

    if not success:
        # The connector reported a failure (e.g. a 4xx / business error). Record
        # it as a failed outcome — NOT a phantom success.
        await _fail(
            f"connector '{connector_name}' action '{connector_action}' failed: {error or 'error'}",
            error_class="ConnectorError",
            response_summary=response_summary,
        )
        return

    # Success — mark executed, back-write the structured outcome, close the chain.
    await store.mark_executed(
        action.id,
        f"external action '{connector_action}' on connector '{connector_name}' "
        f"executed: {response_summary}",
    )
    await _persist_outcome(
        store=store,
        action_id=str(action.id),
        status="executed",
        response_summary=response_summary,
        executed_at=datetime.now(UTC).isoformat(),
    )
    # Close the chain on the SUCCESS path. This is the ONLY terminal on the happy
    # path (every failure path above closed via ``_fail`` and returned), so
    # exactly one ``decision.completed`` lands per call.
    _emit_chain_close(
        passed=True,
        action_outcome="landed",
        error_class=None,
        reason=None,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        user_id=approver,
        causation_id=causation,
        response_summary=response_summary,
    )
    logger.info(
        "external_action: executed action %s → connector %s action %s (ok)",
        action.id,
        connector_name,
        connector_action,
    )


__all__ = ["execute_approved_external_action"]
