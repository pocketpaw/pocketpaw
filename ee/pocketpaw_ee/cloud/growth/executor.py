# ee/pocketpaw_ee/cloud/growth/executor.py — apply an approved growth send.
#
# The apply-on-approve half of the /growth send gate. ``growth.propose`` files
# an Instinct Action carrying a ``_growth_send`` blob; after a human approves
# it, the ee instinct router fires ``execute_approved_growth_send`` here —
# exactly mirroring ``ship.executor.execute_approved_ship_action``. This is the
# ONLY code path that may flip a draft to ``approved`` and enqueue its
# ``growth.dispatch`` job. The job itself is a logging STUB in this slice —
# G-5/G-6 make it actually send and flip the draft to ``sent``.
#
# Guard sequence (order matters, mirroring ship):
#   1. Read the ``_growth_send`` blob. Missing → return (no chain was opened).
#      Schema mismatch → mark_failed + close: a stale pending Action approved
#      after a deploy must fail LOUD, not dispatch misinterpreted.
#   2. Idempotency — a blob already carrying an ``outcome``, or an Action
#      already terminal, does NOT re-fire. Bulk re-approve / retry can never
#      double-dispatch an outbound message.
#   3. Re-check RBAC at execute time, not just at propose: the proposer must
#      STILL hold ``growth.manage`` in the blob's workspace. A since-demoted
#      or since-removed proposer's approved send FAILS CLOSED (learned from
#      admin_proposals via ship: revocation happens between propose and
#      approve). The proposer id is resolved from the ACTION's trigger, NOT
#      the blob's ``requested_by`` — see ``_resolve_proposer`` (security
#      review F2): a blob-supplied id would let whoever wrote the blob choose
#      whose role gets checked.
#   4. Flip the draft proposed→approved through the service's gate seam
#      (``gate_transition`` — the only caller allowed onto a gate-owned edge).
#      A draft that moved meanwhile (rejected / already approved) fails the
#      action instead of dispatching.
#   5. Enqueue ``growth.dispatch`` ``{draft_id, channel}`` on the dedicated
#      ``growth`` arq queue. Enqueue failure → ``store.mark_failed(error=...)``
#      (the draft stays ``approved`` — the approval stands; the failure is
#      recorded on the Action for the operator).
#   6. Back-write the outcome, mark the Action executed/failed, close the
#      Decision-Graph chain exactly once.
#
# CONCURRENCY: same per-action ``asyncio.Lock`` as ship — two concurrent
# invocations on ONE approved action must not double-flip / double-enqueue.
#
# NEVER RAISES — a failure here must not break the approve response. Every
# terminal path goes through the single ``_fail`` chokepoint or the one success
# path, never both.
#
# Created 2026-07-27 (feat/growth-g4): new module.

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.growth.domain import GROWTH_DISPATCH_JOB_NAME, GROWTH_QUEUE_NAME
from pocketpaw_ee.cloud.growth.propose import GROWTH_SEND_PARAM_KEY, GROWTH_SEND_SCHEMA

logger = logging.getLogger(__name__)

# Per-action locks serializing the read-check-write window. Keyed on the Action
# id; module-level is right because the executor is only ever driven from the
# web process's approve path (mirrors ship.executor).
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(action_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(action_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[action_id] = lock
    return lock


def _resolve_proposer(action: Any, blob: dict[str, Any]) -> str | None:
    """Resolve the proposer id to RE-CHECK, from the ACTION — not the blob.

    SECURITY (security review F2) — the blob's ``requested_by`` is data inside
    ``Action.parameters``. Trusting it for the authorization re-check made the
    guard self-referential: whoever could write the blob could also name whose
    role gets checked, so a forged blob naming a known admin would pass the
    re-check and dispatch. The trustworthy record is the Action's own trigger,
    written by ``store.propose`` into the audit row as
    ``f"{trigger.type}:{trigger.source}"`` — the in-process propose helper sets
    ``trigger.source`` from the authenticated caller, and the generic HTTP
    propose route can no longer mint a gated blob at all
    (``instinct.reserved_parameter_key``).

    Returns the proposer id, or ``None`` when it cannot be trusted:
      * no resolvable trigger source, or
      * the blob's ``requested_by`` DISAGREES with it — a tamper signal (the
        two are written from the same value by the propose helper), so we fail
        closed rather than pick a winner.
    """
    trigger = getattr(action, "trigger", None)
    trigger_source = str(getattr(trigger, "source", "") or "")
    if not trigger_source:
        return None
    claimed = str(blob.get("requested_by") or "")
    if claimed and claimed != trigger_source:
        logger.warning(
            "growth: blob requested_by=%r disagrees with the action's trigger "
            "source=%r — refusing to dispatch (tamper signal)",
            claimed,
            trigger_source,
        )
        return None
    return trigger_source


def _already_executed(action: Any, blob: dict[str, Any]) -> bool:
    """Idempotency guard — True when this growth send already fired.

    Either signal is sufficient: the blob carries a back-written ``outcome``,
    or the Action is already terminal. Re-invocation must never double-send.
    """
    if isinstance(blob.get("outcome"), dict):
        return True
    status = getattr(action, "status", None)
    status_value = getattr(status, "value", status)
    return str(status_value) in ("executed", "failed")


# The RBAC action an outbound send requires. Re-checked at EXECUTE time against
# the proposer's CURRENT role, not the role they held at propose (mirrors
# ship.executor's ``ship.manage`` re-check).
_GROWTH_RBAC_ACTION = "growth.manage"


async def _proposer_still_authorized(workspace_id: str, user_id: str) -> bool:
    """True when the proposer STILL holds ``growth.manage`` in this workspace.

    Mirrors ``ship.executor._proposer_still_authorized``: an approved send
    fires ONLY if the proposer holds the required role RIGHT NOW. Demoted or
    removed between proposing and approval → the approved send must NOT go
    out. Loads the proposer's User doc FRESH so ``.workspaces`` reflects the
    current role. Fails CLOSED on any resolution error.
    """
    if not workspace_id or not user_id:
        return False
    try:
        from beanie import PydanticObjectId

        from pocketpaw_ee.cloud.models.user import User as _UserDoc
        from pocketpaw_ee.guards.deps import check_workspace_action

        proposer = await _UserDoc.get(PydanticObjectId(user_id))
        if proposer is None:
            return False
        # Raises Forbidden when the CURRENT role is below the action minimum.
        check_workspace_action(proposer, workspace_id, _GROWTH_RBAC_ACTION)
        return True
    except Exception:  # noqa: BLE001 — denial OR unresolvable proposer fails closed
        logger.warning(
            "growth: proposer no longer authorized for %s (workspace=%s) — failing closed",
            _GROWTH_RBAC_ACTION,
            workspace_id,
            exc_info=True,
        )
        return False


def _emit_chain_close(
    *,
    passed: bool,
    action_outcome: str,
    reason: str | None,
    correlation_id: UUID | None,
    workspace_id: str,
    user_id: str,
    causation_id: UUID | None,
) -> None:
    """Emit the ``decision.completed`` chain close. Best-effort, exactly once."""
    if correlation_id is None:
        return
    try:
        from soul_protocol.spec.journal import Actor

        from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

        actor = Actor(
            kind="agent",
            id=f"user:{user_id or 'unknown'}",
            scope_context=[f"workspace:{workspace_id}"],
        )
        payload: dict[str, Any] = {"passed": passed, "action_outcome": action_outcome}
        if reason:
            payload["reason"] = reason
        record_decision_completed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
            causation_id=causation_id,
        )
    except Exception:  # noqa: BLE001 — chain close is best-effort
        logger.warning(
            "growth: decision.completed emit failed for correlation_id=%s — the "
            "reconciler will catch up",
            correlation_id,
            exc_info=True,
        )


async def _persist_outcome(
    *, store: Any, action_id: str, blob: dict[str, Any], status: str, detail: str
) -> None:
    """Back-write ``{status, detail, executed_at}`` onto the persisted blob.

    Written INSIDE the per-action lock so a concurrent second invocation's
    idempotency check sees it. Best-effort on the persistence itself — the
    Action's own terminal status is the authoritative record.
    """
    try:
        import json as _json

        import aiosqlite

        blob["outcome"] = {
            "status": status,
            "detail": detail[:500],
            "executed_at": datetime.now(UTC).isoformat(),
        }
        params = {GROWTH_SEND_PARAM_KEY: blob}
        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — structured outcome is best-effort
        logger.warning(
            "growth: failed to persist outcome onto action %s (the Action's "
            "terminal status still records it)",
            action_id,
            exc_info=True,
        )


async def _get_pool() -> Any:
    """Resolve the shared arq pool (one Redis; ``_queue_name`` selects the
    ``growth`` queue at enqueue). Module-level indirection so tests inject a
    fake pool by monkeypatching this function — the ship ``pool_factory`` seam
    by another name."""
    from pocketpaw_ee.cloud.chat.runs.arq_executor import _get_pool as _shared_pool

    return await _shared_pool()


async def execute_approved_growth_send(
    action: Any,
    *,
    human_event_id: Any | None = None,
) -> None:
    """Flip the draft to ``approved`` and enqueue its dispatch job.

    Called best-effort from the instinct router's approve paths (single AND
    bulk) after ``store.approve()`` succeeds — exactly like
    ``ship.executor.execute_approved_ship_action``. Never raises. See the
    module header for the guard sequence.
    """
    from pocketpaw.stores import get_instinct_store

    params = getattr(action, "parameters", None) or {}
    blob = params.get(GROWTH_SEND_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not a growth-send Action — no chain was opened, nothing to close.
        return

    action_id = str(getattr(action, "id", "") or "")
    workspace_id = str(blob.get("workspace_id") or "")
    # F2 — the proposer comes off the ACTION's trigger, never the blob's
    # ``requested_by`` (see ``_resolve_proposer``). ``None`` means untrustworthy
    # → the guard sequence fails closed below. The chain actor uses the same
    # resolved value so a tampered blob can't relabel the audit trail either.
    proposer_id = _resolve_proposer(action, blob)
    requested_by = proposer_id or ""
    draft_id = str(blob.get("draft_id") or "")
    channel = str(blob.get("channel") or "")
    corr_raw = blob.get("correlation_id")
    try:
        correlation_id = UUID(str(corr_raw)) if corr_raw else None
    except (TypeError, ValueError):
        correlation_id = None
    causation_id = human_event_id if isinstance(human_event_id, UUID) else None

    store = get_instinct_store(workspace_id=workspace_id or None)

    async def _fail(reason: str) -> None:
        """The single failure chokepoint: outcome + terminal + chain close."""
        await _persist_outcome(
            store=store, action_id=action_id, blob=blob, status="failed", detail=reason
        )
        try:
            await store.mark_failed(action_id, error=reason)
        except Exception:  # noqa: BLE001 — terminal marking is best-effort
            logger.warning("growth: mark_failed failed for action %s", action_id, exc_info=True)
        _emit_chain_close(
            passed=False,
            action_outcome="failed",
            reason=reason,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=requested_by,
            causation_id=causation_id,
        )

    async with _lock_for(action_id):
        # (1) Schema — a stale blob must fail loud, never run misinterpreted.
        if int(blob.get("schema") or 0) != GROWTH_SEND_SCHEMA:
            await _fail("growth send blob schema mismatch — refusing to dispatch")
            return

        # (2) Idempotency — never double-dispatch an outbound message.
        if _already_executed(action, blob):
            logger.info("growth: action %s already executed — not re-firing", action_id)
            return

        # (3) Mandatory fields — fail closed on an unexecutable blob.
        if not workspace_id:
            await _fail("growth send carries no workspace — undispatchable")
            return
        if not draft_id or not channel:
            await _fail("growth send blob is missing draft_id/channel — undispatchable")
            return

        # (4) Authorization re-check at EXECUTE time; fails closed. The
        # proposer id is the ACTION's, not the blob's — an unresolvable or
        # contradicted claim never reaches the role check at all (F2).
        if proposer_id is None:
            await _fail("growth send has no trustworthy proposer — refusing to dispatch")
            return
        if not await _proposer_still_authorized(workspace_id, proposer_id):
            await _fail("proposer is no longer authorized in this workspace")
            return

        # (5) Flip the draft proposed→approved through the gate seam. A draft
        # that moved meanwhile (rejected by hand, gone, already approved) must
        # fail the action, not dispatch.
        try:
            from pocketpaw_ee.cloud.growth import service as growth_service

            await growth_service.gate_transition(workspace_id, draft_id, "approved")
        except Exception as exc:  # noqa: BLE001 — never break the approve response
            logger.warning(
                "growth: draft %s could not be approved (%s)", draft_id, type(exc).__name__
            )
            await _fail(f"draft could not be approved ({type(exc).__name__})")
            return

        # (6) Enqueue the dispatch job on the dedicated growth queue.
        # ``_queue_name`` is arq's selector kwarg (a bare ``queue=`` would be
        # forwarded to the job function and crash it — see jobs/domain.py).
        try:
            pool = await _get_pool()
            await pool.enqueue_job(
                GROWTH_DISPATCH_JOB_NAME,
                draft_id,
                channel,
                _queue_name=GROWTH_QUEUE_NAME,
            )
        except Exception:  # noqa: BLE001 — enqueue failure is a failed outcome
            logger.exception("growth: dispatch enqueue failed for draft %s", draft_id)
            await _fail("dispatch enqueue failed — draft approved but not queued")
            return

        # (7) Success: outcome, terminal, chain close — exactly once.
        detail = f"growth.dispatch enqueued for draft {draft_id} ({channel})"
        await _persist_outcome(
            store=store, action_id=action_id, blob=blob, status="executed", detail=detail
        )
        try:
            await store.mark_executed(action_id, outcome=detail)
        except Exception:  # noqa: BLE001 — terminal marking is best-effort
            logger.warning("growth: mark_executed failed for action %s", action_id, exc_info=True)
        _emit_chain_close(
            passed=True,
            action_outcome="landed",
            reason=None,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=requested_by,
            causation_id=causation_id,
        )


async def mark_growth_send_rejected(action: Any, reason: str) -> None:
    """Reflect an Instinct rejection onto the draft: proposed→rejected.

    Called best-effort from the instinct router's reject paths (single AND
    bulk) AFTER the Action itself is rejected — the dispatch executor never
    runs on reject, so no job is enqueued and nothing sends. The flip goes
    through the gate seam (``rejected`` is a legal target from ``proposed``
    per ``DRAFT_TRANSITIONS``); a draft that already moved is left alone.
    Never raises — a read-model nudge must not break the reject response.
    """
    params = getattr(action, "parameters", None) or {}
    blob = params.get(GROWTH_SEND_PARAM_KEY)
    if not isinstance(blob, dict):
        return
    workspace_id = str(blob.get("workspace_id") or "")
    draft_id = str(blob.get("draft_id") or "")
    if not workspace_id or not draft_id:
        return
    try:
        from pocketpaw_ee.cloud.growth import service as growth_service

        await growth_service.gate_transition(workspace_id, draft_id, "rejected")
    except Exception:  # noqa: BLE001 — best-effort; the Action's rejection is authoritative
        logger.debug(
            "growth: draft %s reject flip failed (non-fatal, reason=%r)",
            draft_id,
            reason,
            exc_info=True,
        )
