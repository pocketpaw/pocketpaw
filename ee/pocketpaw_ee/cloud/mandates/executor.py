# ee/pocketpaw_ee/cloud/mandates/executor.py
# Created: 2026-06-11 (feat/belt-mandates, slice 4 — plan gate + executor).
#
# The apply-on-approve half of the MANDATE plan gate. ``service.trigger_shift``
# proposes the foreman's PlanProposal THROUGH Instinct as a ``belt_plan``
# Action (blob under ``Action.parameters._belt_plan``); after a human approves
# it in The Tray, the ee instinct router fires ``execute_approved_plan`` here —
# EXACTLY mirroring how ``ee.cloud.belt.executor.execute_approved_change`` is
# fired for a ``_code_change`` Action. This function:
#
#   1. Reads the ``_belt_plan`` blob; a missing/schema-mismatched blob fails loud.
#   2. RE-validates at approve time (defense in depth): the mandate still
#      exists, is still ACTIVE, and the charter budget is UNCHANGED since the
#      plan was proposed (a tightened budget refuses a now-over-budget plan).
#   3. Dispatches each approved task as a normal Belt run via the injectable
#      ``TaskDispatcher``. The default ``BusTaskDispatcher`` routes through the
#      EXISTING belt service (``emit_belt_run_updated``) — the genuine external
#      boundary here is the develop-station agent session, exactly as ``gh pr
#      create`` was for the code-change executor, so tests inject a recorder.
#      >>> DEMO-BAR CONCESSION: dispatch announces the run on the belt bus and
#      records the task; wiring an autonomous develop-station runner is the
#      autopilot PR's job. <<<
#   4. Marks the shift ``executing`` → ``done`` (via the mandates service — the
#      sole Beanie importer), appends the shift summary to the mandate's soul
#      (best-effort), and closes the Decision-Graph chain with EXACTLY ONE
#      ``decision.completed`` (the documented chain-doubling trap): every
#      failure path returns right after its single ``_fail`` emit; the success
#      path emits once at the end.
#
# Vocabulary pin (review M1): the chain's SUCCESS terminal is
# ``action_outcome="dispatched"`` — everywhere. The strings "executed" /
# ``mark_executed`` / ``ActionStatus.EXECUTED`` that appear nearby are the
# Instinct STORE's status vocabulary for the Action row, not the chain
# outcome; do not conflate the two.
#
# Reject path: the instinct router owns the chain close on reject (mirroring
# code_change); it calls ``mark_plan_rejected`` here best-effort so the shift
# record reflects the rejection.

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Schema version stamped on the ``_belt_plan`` blob. Bump when the blob shape
# changes so a stale pending plan approved post-deploy fails loud (same
# discipline as the code-change executor's ``_CODE_CHANGE_SCHEMA``).
BELT_PLAN_SCHEMA = 1

# The parameters key the plan blob rides under. The instinct router dispatches
# the approve/reject hooks on this key's presence — the ``belt_plan`` peer of
# ``_code_change`` / ``_pocket_write``.
BELT_PLAN_PARAM_KEY = "_belt_plan"


class TaskDispatcher(Protocol):
    """Injectable dispatch seam — one call per approved task.

    Returns a run reference string (an id the pawprints feed can show). Tests
    inject a recorder; production gets ``BusTaskDispatcher``."""

    async def dispatch(
        self,
        *,
        workspace_id: str,
        mandate_id: str,
        shift_no: int,
        plan_action_id: str,
        index: int,
        task: dict[str, Any],
    ) -> str: ...


class BusTaskDispatcher:
    """Default ``TaskDispatcher`` — announces the run via the EXISTING belt
    service (``emit_belt_run_updated`` on the workspace realtime bus) under a
    synthetic per-task run id ``<plan_action_id>:t<index>``."""

    async def dispatch(
        self,
        *,
        workspace_id: str,
        mandate_id: str,
        shift_no: int,
        plan_action_id: str,
        index: int,
        task: dict[str, Any],
    ) -> str:
        from pocketpaw_ee.cloud.belt import service as belt_service

        run_ref = f"{plan_action_id}:t{index}"
        await belt_service.emit_belt_run_updated(
            workspace_id=workspace_id,
            action_id=run_ref,
            status="dispatched",
            stage="station",
        )
        logger.info(
            "mandate: dispatched task %d of shift %s (mandate=%s) as belt run %s: %s",
            index,
            shift_no,
            mandate_id,
            run_ref,
            str(task.get("title", ""))[:80],
        )
        return run_ref


def _coerce_uuid(raw: Any) -> Any | None:
    """Coerce to UUID or None (mirrors the belt executor helper)."""
    from uuid import UUID

    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


def _emit_chain_close(
    *,
    passed: bool,
    action_outcome: str,
    error_class: str | None,
    reason: str | None,
    correlation_id: Any | None,
    workspace_id: str,
    user_id: str,
    causation_id: Any | None,
    task_count: int | None = None,
    run_refs: list[str] | None = None,
) -> None:
    """Emit the ``decision.completed`` chain-close for a belt_plan run.

    Mirrors the code-change executor's helper. No-ops on a missing
    correlation_id (the abandon-sweeper closes orphans). Best-effort: a
    Decision-Graph wiring failure never breaks the approve response."""
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
    if task_count is not None:
        payload["task_count"] = task_count
    if run_refs:
        payload["run_refs"] = list(run_refs)

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
            "belt_plan decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


async def _mark_shift_safe(
    *,
    workspace_id: str,
    shift_id: str,
    state: str,
    outcome: str | None = None,
) -> None:
    """Best-effort shift-state transition through the mandates service (the
    sole Beanie importer). A Mongo failure must never break the approve path —
    the Instinct outcome is the source of truth."""
    try:
        from pocketpaw_ee.cloud.mandates import service as mandate_service

        await mandate_service.mark_shift(
            workspace_id=workspace_id, shift_id=shift_id, state=state, outcome=outcome
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "mandate: shift %s state write (%s) failed — instinct outcome remains "
            "the source of truth",
            shift_id,
            state,
            exc_info=True,
        )


async def execute_approved_plan(
    action: Any,
    *,
    dispatcher: TaskDispatcher | None = None,
    human_event_id: Any | None = None,
) -> None:
    """Dispatch the shift plan carried by a freshly-approved ``belt_plan``
    Action. Called best-effort from the instinct router's approve paths.

    Never raises. Every terminal path (success or any failure) closes the
    Decision-Graph chain EXACTLY ONCE: failures via the single ``_fail``
    chokepoint (emit + return), success via the single close at the end."""
    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store()
    dispatch: TaskDispatcher = dispatcher or BusTaskDispatcher()

    params = getattr(action, "parameters", None) or {}
    blob = params.get(BELT_PLAN_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not a belt_plan Action — no chain was opened for it here.
        logger.warning("approved action %s carries no _belt_plan blob", action.id)
        return

    correlation_id = _coerce_uuid(blob.get("correlation_id"))
    workspace_id = str(blob.get("workspace_id") or "")
    requested_by = str(blob.get("requested_by") or "")
    mandate_id = str(blob.get("mandate_id") or "")
    shift_id = str(blob.get("shift_id") or "")
    shift_no = int(blob.get("shift_no") or 0)
    causation = _coerce_uuid(human_event_id)

    async def _fail(reason: str, *, error_class: str) -> None:
        """Single failure chokepoint — one mark_failed + ONE chain close, then
        the caller returns. A path can never double-fire the terminal."""
        await store.mark_failed(action.id, reason)
        _emit_chain_close(
            passed=False,
            action_outcome="failed",
            error_class=error_class,
            reason=reason,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=requested_by,
            causation_id=causation,
        )
        await _mark_shift_safe(
            workspace_id=workspace_id,
            shift_id=shift_id,
            state="done",
            outcome=f"plan failed at the gate: {reason}",
        )

    try:
        if blob.get("schema") != BELT_PLAN_SCHEMA:
            await _fail(
                "belt_plan schema mismatch — the plan blob is from an incompatible "
                "build and cannot be dispatched",
                error_class="SchemaMismatch",
            )
            return

        plan = blob.get("plan")
        if not isinstance(plan, dict) or not mandate_id or not shift_id:
            await _fail(
                "belt_plan blob is missing plan/mandate_id/shift_id",
                error_class="MalformedBlob",
            )
            return
        tasks = plan.get("tasks") or []

        # 2. RE-validate at approve time (defense in depth) — the mandate must
        #    still exist, still be ACTIVE, and the budget must be UNCHANGED.
        from pocketpaw_ee.cloud.mandates import service as mandate_service

        try:
            current = await mandate_service.executor_revalidate(workspace_id, mandate_id)
        except Exception:  # noqa: BLE001 — a read failure refuses the dispatch
            await _fail(
                "mandate could not be re-validated at approval time",
                error_class="RevalidateFailed",
            )
            return
        if not current["exists"]:
            await _fail("mandate no longer exists", error_class="MandateGone")
            return
        if not current["active"]:
            await _fail(
                "mandate is paused — plan refused at approval time",
                error_class="MandatePaused",
            )
            return
        budget_now = int(current["budget_max_tasks"])
        budget_at_propose = int(blob.get("budget_max_tasks") or 0)
        if budget_now != budget_at_propose or len(tasks) > budget_now:
            await _fail(
                f"charter budget changed since the plan was proposed "
                f"({budget_at_propose} → {budget_now}; plan carries {len(tasks)} "
                "task(s)) — re-run the shift",
                error_class="BudgetChanged",
            )
            return

        # 3. Dispatch each approved task as a normal Belt run.
        await _mark_shift_safe(workspace_id=workspace_id, shift_id=shift_id, state="executing")
        run_refs: list[str] = []
        for i, task in enumerate(tasks, start=1):
            try:
                ref = await dispatch.dispatch(
                    workspace_id=workspace_id,
                    mandate_id=mandate_id,
                    shift_no=shift_no,
                    plan_action_id=str(action.id),
                    index=i,
                    task=dict(task) if isinstance(task, dict) else {},
                )
            except Exception as exc:  # noqa: BLE001
                await _fail(
                    f"task {i} dispatch failed: {exc}",
                    error_class="DispatchFailed",
                )
                return
            run_refs.append(ref)

        # 4. Success terminal — one mark_executed, one shift transition, one
        #    soul append, ONE chain close.
        outcome_text = (
            f"Shift {shift_no}: dispatched {len(run_refs)} task(s) as belt runs "
            f"({', '.join(run_refs)})"
        )
        await store.mark_executed(action.id, outcome_text)
        await _mark_shift_safe(
            workspace_id=workspace_id,
            shift_id=shift_id,
            state="done",
            outcome=outcome_text,
        )
        try:
            from pocketpaw_ee.cloud.mandates import soul_link

            await soul_link.remember_shift(
                str(blob.get("soul_path") or "") or None,
                f"Mandate shift {shift_no}: plan approved; {outcome_text}",
            )
        except Exception:  # noqa: BLE001 — soul append is best-effort
            logger.debug("mandate: soul append failed (non-fatal)", exc_info=True)

        _emit_chain_close(
            passed=True,
            action_outcome="dispatched",
            error_class=None,
            reason=None,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=requested_by,
            causation_id=causation,
            task_count=len(run_refs),
            run_refs=run_refs,
        )
        logger.info(
            "mandate: dispatched belt_plan action %s (mandate=%s, shift=%s, %d task(s))",
            action.id,
            mandate_id,
            shift_no,
            len(run_refs),
        )
    except Exception:  # noqa: BLE001 — never let the executor break approve
        logger.warning(
            "mandate: belt_plan execution crashed for action %s", action.id, exc_info=True
        )
        try:
            await _fail(
                "belt_plan executor crashed — re-run the shift", error_class="ExecutorCrash"
            )
        except Exception:  # noqa: BLE001 — a second failure must never mask the first
            logger.debug("mandate: crash-path _fail itself failed", exc_info=True)


async def mark_plan_rejected(action: Any, reason: str) -> None:
    """Reflect a gate REJECTION onto the shift record (state=done, outcome
    carries the reason) + soul append. The instinct ROUTER owns the chain close
    on reject (mirroring code_change) — this only updates the read model.
    Best-effort, never raises."""
    try:
        params = getattr(action, "parameters", None) or {}
        blob = params.get(BELT_PLAN_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        shift_no = int(blob.get("shift_no") or 0)
        outcome = f"Shift {shift_no}: plan rejected at the gate" + (
            f" — {reason}" if reason else ""
        )
        await _mark_shift_safe(
            workspace_id=str(blob.get("workspace_id") or ""),
            shift_id=str(blob.get("shift_id") or ""),
            state="done",
            outcome=outcome,
        )
        from pocketpaw_ee.cloud.mandates import soul_link

        await soul_link.remember_shift(
            str(blob.get("soul_path") or "") or None, f"Mandate {outcome}"
        )
    except Exception:  # noqa: BLE001
        logger.debug("mandate: mark_plan_rejected failed (non-fatal)", exc_info=True)


__all__ = [
    "BELT_PLAN_PARAM_KEY",
    "BELT_PLAN_SCHEMA",
    "BusTaskDispatcher",
    "TaskDispatcher",
    "execute_approved_plan",
    "mark_plan_rejected",
]
