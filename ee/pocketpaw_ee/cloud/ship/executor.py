# ee/pocketpaw_ee/cloud/ship/executor.py — apply an approved /ship action.
#
# The apply-on-approve half of the /ship gate. ``ship.propose`` files an Instinct
# Action carrying a ``_ship_action`` blob; after a human approves it, the ee
# instinct router fires ``execute_approved_ship_action`` here — exactly mirroring
# how ``external_actions.executor.execute_approved_external_action`` and
# ``admin_proposals.executor.execute_approved_admin_action`` are fired. This is
# the ONLY code path in /ship that may call the engine's destructive verbs.
#
# The guard sequence (order matters, every step is load-bearing):
#
#   1. Read the ``_ship_action`` blob. Missing → return (no chain was opened).
#      Schema mismatch → mark_failed + close, nothing fires: a stale pending
#      Action approved after a deploy must fail LOUD, not run misinterpreted.
#   2. Re-validate ``params_hash`` off the PERSISTED blob. An edit between
#      propose and approve → mark_failed + close, nothing fires. A human approved
#      a SPECIFIC teardown; a tampered blob must never destroy something else.
#   3. Idempotency guard — a blob that already carries an ``outcome``, or an
#      Action already terminal, does NOT re-fire. Bulk re-approve / retry can
#      never double-destroy.
#   4. Re-check RBAC at execute time, not just at propose: the proposer must
#      STILL hold ``ship.manage`` in the blob's workspace, resolved off a freshly
#      loaded User doc. A since-demoted or since-removed proposer's approved
#      teardown FAILS CLOSED. (Learned from admin_proposals: propose-time
#      authorization is not enough — revocation happens in between.)
#   5. Run the verb through ``ship.engine.box_session``, which resolves the box's
#      SSH credential FRESH (decrypt → 0600 temp file → shred in a finally). No
#      secret is ever read off the blob.
#   6. Back-write the outcome, mark the Action executed/failed, close the chain
#      exactly once.
#
# CONCURRENCY (paid for in production by admin_proposals): two concurrent
# invocations on ONE approved action both passed the read-then-write guard and
# double-fired the write. Two defences here: a per-action ``asyncio.Lock`` so the
# read-check-write window is serialized in-process, and the outcome back-write
# inside that window so the second caller's idempotency check sees it.
#
# NEVER RAISES — a failure here must not break the approve response. Every
# terminal path goes through the single ``_fail`` chokepoint or the one success
# path, never both (double-closing a Decision chain corrupts it).
#
# Created 2026-07-22 (feat/ship-4-agent-surface, SHIP-4): new module.

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pocketpaw_ee.cloud.ship.propose import (
    SHIP_ACTION_PARAM_KEY,
    SHIP_ACTION_SCHEMA,
    compute_params_hash,
)

logger = logging.getLogger(__name__)

# Per-action locks serializing the read-check-write window. Keyed on the Action
# id; a module-level dict is right because the executor is only ever driven from
# the web process's approve path.
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(action_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(action_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[action_id] = lock
    return lock


def _already_executed(action: Any, blob: dict[str, Any]) -> bool:
    """Idempotency guard — True when this /ship action already fired.

    Either signal is sufficient: the blob carries a back-written ``outcome``, or
    the Action is already terminal. Re-invocation must never double-destroy.
    """
    if isinstance(blob.get("outcome"), dict):
        return True
    status = getattr(action, "status", None)
    status_value = getattr(status, "value", status)
    return str(status_value) in ("executed", "failed")


# The RBAC action a destructive /ship verb requires. Re-checked at EXECUTE time
# against the proposer's CURRENT role, not the role they held at propose.
_SHIP_RBAC_ACTION = "ship.manage"


async def _proposer_still_authorized(workspace_id: str, user_id: str) -> bool:
    """True when the proposer STILL holds ``ship.manage`` in this workspace.

    THE load-bearing rule, mirroring ``admin_proposals.executor._recheck_rbac``:
    an approved teardown fires ONLY if the proposer holds the required role RIGHT
    NOW. Demoted or removed between proposing and approval → the approved action
    must NOT execute. Loads the proposer's User doc FRESH so ``.workspaces``
    reflects the current role. Fails CLOSED on any resolution error.
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
        check_workspace_action(proposer, workspace_id, _SHIP_RBAC_ACTION)
        return True
    except Exception:  # noqa: BLE001 — denial OR unresolvable proposer fails closed
        logger.warning(
            "ship: proposer no longer authorized for %s (workspace=%s) — failing closed",
            _SHIP_RBAC_ACTION,
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
            "ship: decision.completed emit failed for correlation_id=%s — the "
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
        params = {SHIP_ACTION_PARAM_KEY: blob}
        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — structured outcome is best-effort
        logger.warning(
            "ship: failed to persist outcome onto action %s (the Action's "
            "terminal status still records it)",
            action_id,
            exc_info=True,
        )


async def _run_verb(blob: dict[str, Any]) -> tuple[bool, str]:
    """Run the approved verb against the live box. Returns ``(ok, detail)``.

    Resolves the box FRESH through the ship store + ``engine.box_session`` — no
    credential is read off the blob. Raises nothing: an engine failure is
    returned as ``(False, detail)`` so the caller's single ``_fail`` chokepoint
    handles it.
    """
    from pocketpaw_ee.cloud.ship import engine
    from pocketpaw_ee.cloud.ship import store as ship_store

    workspace_id = str(blob.get("workspace_id") or "")
    verb = str(blob.get("verb") or "")
    box_id = str(blob.get("box_id") or "")
    app_id = str(blob.get("app_id") or "")
    params = dict(blob.get("params") or {})

    app = None
    if app_id:
        app = await ship_store.get_app(workspace_id, app_id)
        if app is None:
            return False, "app no longer exists in this workspace"
        box_id = box_id or app.box_id

    box = await ship_store.get_box(workspace_id, box_id)
    if box is None:
        return False, "box no longer exists in this workspace"

    try:
        async with engine.box_session(box) as session:
            if verb == "destroy_box":
                # Tear down every app the box carries, then the box itself is
                # released by the provider teardown (SHIP-6 owns provider
                # destroy; here we make the box's apps gone).
                for existing in await ship_store.list_apps(workspace_id, box_id=box_id):
                    await session.engine.destroy(existing.name)
                await ship_store.set_status(box, "destroyed")
                return True, f"destroyed box {box_id}"
            if verb == "destroy_app":
                await session.engine.destroy(app.name)  # type: ignore[union-attr]
                await ship_store.set_app_status(app, "destroyed")  # type: ignore[arg-type]
                return True, f"destroyed app {app.name}"  # type: ignore[union-attr]
            if verb == "rollback":
                image = str(params.get("image") or "")
                if not image:
                    return False, "rollback requires a target image"
                result = await session.engine.rollback(app.name, image)  # type: ignore[union-attr]
                return True, f"rolled back to {result.image}"
            if verb == "deploy_app":
                from pocketpaw_ee.ship_engine.port import DeployRequest

                image = str(params.get("image") or getattr(app, "image", "") or "")
                if not image:
                    return False, "deploy requires an image"
                result = await session.engine.deploy_app(
                    DeployRequest(app=app.name, image=image)  # type: ignore[union-attr]
                )
                await ship_store.record_app_deployed(
                    app,  # type: ignore[arg-type]
                    image=result.image,
                    app_url=result.app_url,
                )
                return True, f"deployed {result.image}"
        return False, f"unsupported gated verb {verb!r}"
    except Exception as exc:  # noqa: BLE001 — any engine failure is an outcome
        logger.warning("ship: approved verb %s failed (%s)", verb, type(exc).__name__)
        return False, f"engine call failed ({type(exc).__name__})"


async def execute_approved_ship_action(
    action: Any,
    *,
    human_event_id: Any | None = None,
) -> None:
    """Execute the /ship verb carried by a freshly-approved Action.

    Called best-effort from the instinct router's approve paths after
    ``store.approve()`` succeeds. Never raises. See the module header for the
    full guard sequence and the concurrency discipline.
    """
    from pocketpaw.stores import get_instinct_store

    params = getattr(action, "parameters", None) or {}
    blob = params.get(SHIP_ACTION_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not a /ship Action — no chain was opened, nothing to close.
        return

    action_id = str(getattr(action, "id", "") or "")
    workspace_id = str(blob.get("workspace_id") or "")
    requested_by = str(blob.get("requested_by") or "")
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
            logger.warning("ship: mark_failed failed for action %s", action_id, exc_info=True)
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
        if int(blob.get("schema") or 0) != SHIP_ACTION_SCHEMA:
            await _fail("ship action blob schema mismatch — refusing to execute")
            return

        # (2) Params hash — refuse a post-propose edit.
        expected = str(blob.get("params_hash") or "")
        actual = compute_params_hash(str(blob.get("verb") or ""), dict(blob.get("params") or {}))
        if not expected or expected != actual:
            await _fail("ship action params changed after approval — refusing to execute")
            return

        # (3) Idempotency — never double-destroy.
        if _already_executed(action, blob):
            logger.info("ship: action %s already executed — not re-firing", action_id)
            return

        # (4) Authorization re-check at EXECUTE time; fails closed.
        if not workspace_id:
            await _fail("ship action carries no workspace — unexecutable")
            return
        if not await _proposer_still_authorized(workspace_id, requested_by):
            await _fail("proposer is no longer authorized in this workspace")
            return

        # (5) Run it. ``_run_verb`` already converts engine failures into a
        # ``(False, detail)`` value; this catch covers a PROGRAMMING error inside
        # it (a bad attribute, a broken import), which must still not break the
        # approve response — the module's never-raises contract is absolute.
        try:
            ok, detail = await _run_verb(blob)
        except Exception:  # noqa: BLE001 — never break the approve response
            logger.exception("ship: verb execution raised unexpectedly for %s", action_id)
            await _fail("ship action failed unexpectedly during execution")
            return
        if not ok:
            await _fail(detail)
            return

        # (6) Success: outcome, terminal, chain close — exactly once.
        await _persist_outcome(
            store=store, action_id=action_id, blob=blob, status="executed", detail=detail
        )
        try:
            await store.mark_executed(action_id, outcome=detail)
        except Exception:  # noqa: BLE001 — terminal marking is best-effort
            logger.warning("ship: mark_executed failed for action %s", action_id, exc_info=True)
        _emit_chain_close(
            passed=True,
            action_outcome="landed",
            reason=None,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=requested_by,
            causation_id=causation_id,
        )
