# ee/cloud/pocket_proposals/executor.py — apply an approved starter-Pocket create.
# Created: 2026-06-19 (SZD-5b — _pocket_create Instinct proposal type).
#
# What this module does (the apply-on-approve half of the Pocket-create gate): the
# propose helper (``pocket_proposals.propose.propose_pocket``) files an Instinct
# Action carrying a ``_pocket_create`` blob THROUGH Instinct (the human
# approve/reject layer). After a human approves the Action, the ee instinct
# router's ``approve_action`` fires ``execute_approved_pocket_create`` here —
# exactly mirroring how ``fabric_proposals.executor.execute_approved_fabric_objects``
# is fired for a gated Fabric write. This function:
#
#   1. Reads the ``_pocket_create`` blob from ``action.parameters``. A missing blob
#      → return (no chain was opened, nothing to close). A schema-mismatched blob →
#      mark_failed + close the chain, return.
#   2. Workspace guard — an empty ``workspace_id`` on the blob → mark_failed +
#      close (a Pocket create with no tenant to scope it to is a tenancy hole).
#   3. Idempotency guard — if the Action is already terminal (executed / failed) or
#      the blob already carries an ``outcome``, the create is NOT re-run.
#      Re-approve / retry never double-creates.
#   4. Validates the staged spec at the entry to the create path:
#      ``body = CreatePocketRequest.model_validate(blob["pocket_spec"])`` — a
#      structurally invalid spec fails CLEANLY (mark_failed + close), not silently.
#      Then ``await pockets.service.create(workspace_id, user_id, body)``, scoped
#      by the blob's top-level ``workspace_id`` / owned by its top-level
#      ``user_id`` (NOT anything inside ``pocket_spec`` — tenancy/owner are
#      un-editable).
#   5. Back-writes the outcome ``{status, pocket_id, name, executed_at}`` onto the
#      persisted blob via the direct-SQL pattern.
#   6. Records the result on the Action (``mark_executed`` / ``mark_failed``) and
#      CLOSES the Decision-Graph chain exactly once.
#
# EXACTLY-ONE-TERMINAL discipline (critical): on APPROVE the EXECUTOR owns the
# ``decision.completed`` chain close — the router does NOT emit it (mirrors the
# Fabric-objects executor). On REJECT the ROUTER owns the close and the executor
# never runs. Every terminal path here goes through the single ``_fail`` chokepoint
# (failure) or the one success emit at the end — never both.
#
# Never raises — a failure here must not break the approve response. The router
# wraps the call too; this is belt-and-braces. A create error is captured as
# ``status=failed`` with a ``failed`` terminal, NOT re-raised.
#
# Security (this code creates a Pocket in a tenant's workspace on approval):
#   * tenancy + owner are read off the blob's SEPARATE top-level ``workspace_id`` /
#     ``user_id`` fields — NEVER off ``pocket_spec`` (which the correction flow can
#     edit). An empty workspace_id is refused (a tenancy hole). The router's
#     ``_assert_pocket_create_workspace`` is the primary gate; this is
#     belt-and-braces.
#   * the write primitive is the SHIPPED ``pockets.service.create`` — this module
#     does NOT touch the Beanie document directly, so it inherits the proven
#     workspace-scoped, validate-at-entry create path.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.pocket_proposals.propose import (
    POCKET_CREATE_PARAM_KEY,
    POCKET_CREATE_SCHEMA,
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
    """Back-write the create outcome onto the persisted ``_pocket_create`` blob.

    Direct SQL update — the same pattern the Fabric-objects executor's
    ``_persist_outcome`` uses. The blob's ``outcome`` carries the created pocket id
    + name + timestamp so a reader (audit, a re-invocation idempotency check) sees
    the result structurally. Best-effort: a write failure leaves the blob without
    the structured outcome but the free-text ``mark_executed`` / ``mark_failed``
    outcome still records it.
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(POCKET_CREATE_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["outcome"] = {"status": status, "executed_at": executed_at, **summary}
        params[POCKET_CREATE_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — back-write is best-effort
        logger.warning(
            "pocket_create: failed to persist outcome onto action %s — the "
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
    """Emit the ``decision.completed`` chain-close for a Pocket create.

    Mirrors ``fabric_proposals.executor._emit_chain_close`` — the executor owns the
    chain close on the approve path. ``correlation_id`` is read off the blob;
    ``causation_id`` is the ``human.corrected`` event the router emitted just
    before approval so the terminal chains back to the human approval.

    Returns early when ``correlation_id`` is None (a blob with a malformed /
    missing id): there is no chain to close. Best-effort: a Decision-Graph wiring
    failure must never break the approve response — the journal write is the source
    of truth; the Slice 4 reconciler is the safety net.
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
            "pocket_create decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


def _already_executed(action: Any, blob: dict[str, Any]) -> bool:
    """Idempotency guard — True when this Pocket create already ran.

    Two signals, either is sufficient:
      * the blob already carries an ``outcome`` (a prior run back-wrote it), or
      * the Action is already in a terminal state (executed / failed).

    Re-invocation (bulk re-approve, retry) must never re-run the create — a Pocket
    create has no natural idempotency key (each call mints a fresh pocket id), so
    the Action's terminal ``status`` + the back-written outcome are the ONLY guard
    against a double-create. The Action's ``status`` is the authoritative gate, and
    the back-written outcome is the belt-and-braces signal in case status reads are
    stale.
    """
    if isinstance(blob.get("outcome"), dict):
        return True
    status = getattr(action, "status", None)
    status_value = getattr(status, "value", status)
    return str(status_value) in ("executed", "failed")


async def execute_approved_pocket_create(
    action: Any,
    *,
    human_event_id: Any | None = None,
) -> None:
    """Create the starter Pocket carried by a freshly-approved Action.

    Called best-effort from the instinct router's ``approve_action`` /
    ``bulk_approve_actions`` after ``store.approve()`` succeeds — the same hook
    shape the Fabric-objects executor uses. ``action`` is the approved Action.

    ``human_event_id`` is the id of the ``human.corrected`` event the router
    emitted just before calling this — threaded through so the terminal
    ``decision.completed`` event can chain its ``causation_id`` back to the
    approval, completing the causal walk ``agent.proposed → human.corrected →
    decision.completed``. ``None`` is tolerated (the chain still folds via the
    shared ``correlation_id``).

    Never raises — a failure here must not break the approve response. The router
    wraps the call too; this is belt-and-braces. The Action is marked executed on a
    successful create or failed with a clear outcome on any error, and the
    Decision-Graph chain is closed exactly once (success → landed, failure →
    failed).
    """
    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store()
    params = getattr(action, "parameters", None) or {}
    blob = params.get(POCKET_CREATE_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not a pocket-create Action at all — no chain was opened for it, so there
        # is nothing to close. Return without a terminal emit.
        logger.warning("approved action %s carries no _pocket_create blob", action.id)
        return

    # Read the chain ids + tenancy/owner off the blob up front so EVERY terminal
    # path can close the chain it opened. Tenancy/owner come from the SEPARATE
    # top-level fields — NEVER from ``pocket_spec`` (which the correction flow can
    # edit). A malformed / missing correlation id → None and the close no-ops (the
    # Slice 4 abandon-sweeper closes any chain left open).
    correlation_id = _coerce_uuid(blob.get("correlation_id"))
    workspace_id = str(blob.get("workspace_id") or "")
    owner_user_id = str(blob.get("user_id") or "")
    approver = str(getattr(action, "approved_by", "") or "") or owner_user_id or "system"
    causation = _coerce_uuid(human_event_id)

    async def _fail(reason: str, *, error_class: str) -> None:
        """Mark the Action failed AND close the chain with one terminal — the
        single failure-path chokepoint so a path can never both fail and
        double-fire the terminal."""
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

    # Schema guard — a stale blob from an incompatible build fails loud rather than
    # creating a misinterpreted Pocket.
    if blob.get("schema") != POCKET_CREATE_SCHEMA:
        await _fail(
            "pocket-create schema mismatch — the blob is from an incompatible "
            "build and cannot be executed",
            error_class="SchemaMismatch",
        )
        return

    # Tenancy — a Pocket create with no workspace is a tenancy hole. The Pocket is
    # created scoped by workspace_id; fail loud so a malformed blob is recorded, not
    # silently created unscoped.
    if not workspace_id:
        await _fail(
            "pocket-create blob carries no workspace_id — cannot scope the create",
            error_class="MissingWorkspace",
        )
        return
    if not owner_user_id:
        await _fail(
            "pocket-create blob carries no user_id — cannot assign an owner",
            error_class="MissingOwner",
        )
        return

    # Idempotency — never re-run the create on a re-invocation.
    if _already_executed(action, blob):
        logger.info(
            "pocket_create: action %s already executed (idempotency guard) — "
            "skipping the Pocket create",
            action.id,
        )
        return

    pocket_spec = blob.get("pocket_spec")
    if not isinstance(pocket_spec, dict):
        await _fail(
            "pocket-create blob carries no pocket_spec to create",
            error_class="MalformedBlob",
        )
        return

    # Validate the staged spec at the entry to the create path — a structurally
    # invalid spec (e.g. a missing/blank name) fails CLEANLY here rather than
    # blowing up inside the service. ``CreatePocketRequest`` carries the camelCase
    # aliases (``rippleSpec`` / ``templateSlug``) with populate_by_name=True, so a
    # spec authored with either spelling resolves.
    try:
        from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

        body = CreatePocketRequest.model_validate(pocket_spec)
    except Exception as exc:  # noqa: BLE001 — a bad spec is a failed outcome, not a crash
        await _fail(
            f"pocket-create spec is invalid: {exc}",
            error_class=type(exc).__name__,
        )
        return

    # Create the Pocket through the SHIPPED service, scoped by the blob's top-level
    # workspace_id / owned by its top-level user_id. Any failure (validation inside
    # the service, store error) is captured as a failed outcome — NEVER re-raised
    # into the router.
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        created = await pockets_service.create(workspace_id, owner_user_id, body)
    except Exception as exc:  # noqa: BLE001 — never let a create break approve
        logger.warning(
            "pocket_create: create crashed for action %s (workspace=%s)",
            action.id,
            workspace_id,
            exc_info=True,
        )
        await _fail(
            f"pocket create failed: {exc}",
            error_class=type(exc).__name__,
        )
        return

    # The pockets wire dict carries the id under the legacy ``_id`` key (with
    # ``id`` accepted as a fallback for any future wire-shape change) and the
    # tenant under ``workspace``.
    created_dict = created or {}
    pocket_id = str(created_dict.get("_id") or created_dict.get("id") or "")
    pocket_name = str(created_dict.get("name") or body.name)
    summary = {"pocket_id": pocket_id, "name": pocket_name}

    # Success — mark executed, back-write the structured outcome, close the chain.
    await store.mark_executed(
        action.id,
        f"starter Pocket created: {pocket_name!r} (id={pocket_id})",
    )
    await _persist_outcome(
        store=store,
        action_id=str(action.id),
        status="executed",
        summary=summary,
        executed_at=datetime.now(UTC).isoformat(),
    )
    # Close the chain on the SUCCESS path. This is the ONLY terminal on the happy
    # path (every failure path above closed via ``_fail`` and returned), so exactly
    # one ``decision.completed`` lands per create.
    _emit_chain_close(
        passed=True,
        action_outcome="landed",
        error_class=None,
        reason=None,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        user_id=approver,
        causation_id=causation,
        summary=f"created Pocket {pocket_name!r} (id={pocket_id})",
    )
    logger.info(
        "pocket_create: executed action %s → created Pocket %s (%r) (ok)",
        action.id,
        pocket_id,
        pocket_name,
    )


__all__ = ["execute_approved_pocket_create"]
