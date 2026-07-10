# ee/cloud/fabric_conflicts/executor.py — apply an approved conflict-stewardship choice.
# Created: 2026-07-10 (FST-6 — the conflict lifecycle: _fabric_conflict proposal type).
#
# What this module does (the apply-on-approve half of the conflict gate): the
# propose helper (``fabric_conflicts.propose``) stages an un-rankable Fabric
# conflict as an Instinct Action carrying a ``_fabric_conflict`` blob. After a
# human approves it (optionally editing ``resolution.chosen_statement_id`` to a
# rival via the approve-with-edits path), the ee instinct router fires
# ``execute_approved_fabric_conflict`` here — exactly mirroring the
# instinct-rule gate (the PRECEDENT MIRRORED; see propose.py). This function:
#
#   1. Reads the ``_fabric_conflict`` blob. Missing blob → return (no chain was
#      opened for it). Schema mismatch → mark_failed + close, return.
#   2. Tenancy guard — an empty ``workspace_id`` → mark_failed + close (a pin
#      with no tenant to scope it is a tenancy hole). Subject guard — missing
#      ``object_id`` / ``property`` → mark_failed + close.
#   3. Idempotency guard — a terminal Action (executed/failed) or a blob
#      already carrying an ``outcome`` is NOT re-run: re-approve / retry never
#      double-pins.
#   4. Choice validation at the entry to the write path — the (possibly
#      edited) ``resolution.chosen_statement_id`` MUST be one of the blob's
#      staged ``choices``; anything else fails CLEANLY (an edit cannot smuggle
#      an arbitrary statement id in). Belt-and-braces: the OSS verb re-checks
#      that the statement belongs to exactly this (object, property) within
#      the workspace scope.
#   5. THE CHOICE → VERB MAPPING: **PIN the chosen statement** via the
#      canonical OSS steward verb ``FabricStore.pin_statement`` (workspace-
#      scoped store, ISO-1). PIN is the durable "this one wins" — the
#      resolver's pinned short-circuit settles today's rivals AND future ones,
#      and the losers stay auditable (IGNORE-the-rival would strike history
#      and only settle today's conflict). In enforce the verb also writes the
#      new winner into the flat cache; in shadow the pin lands on the
#      statement layer only — exactly the steward-verb contract.
#      REJECT never reaches this module: the router owns the reject-close and
#      the policy's provisional winner simply stands (no statement change).
#   6. Back-writes the outcome ``{status, pinned_statement_id, value,
#      executed_at}`` onto the blob, marks the Action executed/failed, and
#      closes the Decision-Graph chain exactly once.
#
# EXACTLY-ONE-TERMINAL discipline: on APPROVE the EXECUTOR owns the
# ``decision.completed`` close — the router does NOT emit it. On REJECT the
# ROUTER owns the close and the executor never runs. Every terminal path goes
# through the single ``_fail`` chokepoint or the one success emit at the end.
#
# Never raises — a failure here must not break the approve response. The
# router wraps the call too; this is belt-and-braces.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.fabric_conflicts.propose import (
    FABRIC_CONFLICT_PARAM_KEY,
    FABRIC_CONFLICT_SCHEMA,
)

logger = logging.getLogger(__name__)


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


async def _persist_outcome(
    *,
    store: Any,
    action_id: str,
    status: str,
    summary: dict[str, Any],
    executed_at: str,
) -> None:
    """Back-write the pin outcome onto the persisted ``_fabric_conflict`` blob.

    Direct SQL update — the same pattern the instinct-rule executor uses. The
    blob's ``outcome`` carries the pinned statement id + value so a reader
    (audit, the idempotency guard, a "disputed facts" view) sees the result
    structurally. Best-effort: a write failure leaves the free-text
    ``mark_executed`` / ``mark_failed`` outcome as the record.
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(FABRIC_CONFLICT_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["outcome"] = {"status": status, "executed_at": executed_at, **summary}
        params[FABRIC_CONFLICT_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — back-write is best-effort
        logger.warning(
            "fabric_conflict: failed to persist outcome onto action %s — the "
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
    summary: str | None = None,
) -> None:
    """Emit the ``decision.completed`` chain-close for a conflict stewardship.

    Mirrors ``instinct_rule_proposals.executor._emit_chain_close`` — the
    executor owns the close on the approve path. Returns early when
    ``correlation_id`` is None (no chain to close). Best-effort.
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
    payload: dict[str, Any] = {
        "passed": passed,
        "action_outcome": action_outcome,
    }
    if error_class:
        payload["error_class"] = error_class
    if reason:
        payload["reason"] = reason
    if summary:
        payload["summary"] = summary

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
            "fabric_conflict decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


def _already_executed(action: Any, blob: dict[str, Any]) -> bool:
    """Idempotency guard — True when this stewardship choice already ran.

    Two signals, either sufficient: the blob carries an ``outcome`` (a prior
    run back-wrote it), or the Action is already terminal (executed/failed).
    A PIN is idempotent at the store layer, but re-running would still emit a
    duplicate chain terminal — so the guard mirrors the precedent exactly.
    """
    if isinstance(blob.get("outcome"), dict):
        return True
    status = getattr(action, "status", None)
    status_value = getattr(status, "value", status)
    return str(status_value) in ("executed", "failed")


async def execute_approved_fabric_conflict(
    action: Any,
    *,
    human_event_id: Any | None = None,
) -> None:
    """PIN the statement a freshly-approved stewardship Action chose.

    Called best-effort from the instinct router's ``approve_action`` /
    ``bulk_approve_actions`` after ``store.approve()`` succeeds — the same
    hook shape the instinct-rule executor uses. ``human_event_id`` threads
    the router's ``human.corrected`` event id into the terminal
    ``decision.completed`` causation. Never raises.
    """
    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store()
    params = getattr(action, "parameters", None) or {}
    blob = params.get(FABRIC_CONFLICT_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not a conflict-stewardship Action — no chain was opened for it here.
        logger.warning("approved action %s carries no _fabric_conflict blob", action.id)
        return

    correlation_id = _coerce_uuid(blob.get("correlation_id"))
    workspace_id = str(blob.get("workspace_id") or "")
    object_id = str(blob.get("object_id") or "")
    property_name = str(blob.get("property") or "")
    approver = str(getattr(action, "approved_by", "") or "") or "steward"
    causation = _coerce_uuid(human_event_id)

    async def _fail(reason: str, *, error_class: str) -> None:
        """Mark the Action failed AND close the chain with one terminal — the
        single failure-path chokepoint (never both fail and double-fire)."""
        await store.mark_failed(action.id, reason)
        await _persist_outcome(
            store=store,
            action_id=str(action.id),
            status="failed",
            summary={"reason": reason},
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
            summary=reason,
        )

    # Schema guard — a stale blob from an incompatible build fails loud rather
    # than pinning a misinterpreted statement.
    if blob.get("schema") != FABRIC_CONFLICT_SCHEMA:
        await _fail(
            "fabric-conflict schema mismatch — the blob is from an incompatible "
            "build and cannot be executed",
            error_class="SchemaMismatch",
        )
        return

    # Tenancy — a pin with no workspace to scope it is a tenancy hole.
    if not workspace_id:
        await _fail(
            "fabric-conflict blob carries no workspace_id — cannot scope the pin",
            error_class="MissingWorkspace",
        )
        return
    if not object_id or not property_name:
        await _fail(
            "fabric-conflict blob carries no object_id/property — nothing to pin",
            error_class="MalformedBlob",
        )
        return

    # Idempotency — never re-run on a re-invocation (bulk re-approve, retry).
    if _already_executed(action, blob):
        logger.info(
            "fabric_conflict: action %s already executed (idempotency guard) — skipping the pin",
            action.id,
        )
        return

    # The human's (possibly edited) choice — validated against the STAGED
    # choices so an edit can never smuggle in an arbitrary statement id.
    resolution_spec = blob.get("resolution")
    chosen = ""
    if isinstance(resolution_spec, dict):
        chosen = str(resolution_spec.get("chosen_statement_id") or "")
    if not chosen:
        await _fail(
            "fabric-conflict blob carries no resolution.chosen_statement_id",
            error_class="MalformedBlob",
        )
        return
    staged_ids = {
        str(c.get("statement_id") or "") for c in (blob.get("choices") or []) if isinstance(c, dict)
    }
    if chosen not in staged_ids:
        await _fail(
            f"chosen statement {chosen!r} is not one of the staged choices — "
            "refusing to pin an unstaged statement",
            error_class="InvalidChoice",
        )
        return

    # THE VERB: pin the chosen statement through the canonical OSS steward
    # verb on the workspace-scoped store. Any failure (statement vanished, was
    # deprecated by a CORRECT in the meantime, store error) is a failed
    # outcome — NEVER re-raised into the router.
    try:
        from pocketpaw.stores import get_fabric_store

        fabric = get_fabric_store(workspace_id=workspace_id or None)
        resolution = await fabric.pin_statement(
            object_id, property_name, chosen, workspace_id=workspace_id
        )
    except Exception as exc:  # noqa: BLE001 — never let the pin break approve
        logger.warning(
            "fabric_conflict: pin crashed for action %s (workspace=%s)",
            action.id,
            workspace_id,
            exc_info=True,
        )
        await _fail(f"pin failed: {exc}", error_class=type(exc).__name__)
        return

    summary = {
        "pinned_statement_id": chosen,
        "object_id": object_id,
        "property": property_name,
        "value": resolution.value,
    }

    # Success — mark executed, back-write the structured outcome, close the
    # chain. This is the ONLY terminal on the happy path.
    await store.mark_executed(
        action.id,
        f"pinned statement {chosen} as the winner for {property_name!r} (object {object_id})",
    )
    await _persist_outcome(
        store=store,
        action_id=str(action.id),
        status="executed",
        summary=summary,
        executed_at=datetime.now(UTC).isoformat(),
    )
    _emit_chain_close(
        passed=True,
        action_outcome="landed",
        error_class=None,
        reason=None,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        user_id=approver,
        causation_id=causation,
        summary=f"pinned statement {chosen} for {property_name!r} on {object_id}",
    )
    logger.info(
        "fabric_conflict: executed action %s → pinned %s on (%s, %r) (ok)",
        action.id,
        chosen,
        object_id,
        property_name,
    )


__all__ = ["execute_approved_fabric_conflict"]
