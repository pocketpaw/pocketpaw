# ee/cloud/mission_control/service.py
# Created: 2026-05-13 (feat/mission-control-facade) — façade service that
# composes Instinct (Nudges + Pawprints) and the in-process activity buffer
# into Mission Control's unified WorkItem shape. PR 1 of three.
# Updated: 2026-05-13 (feat/mission-control-cleanup) — lifted the 501 stubs
# on bulk-reassign + bulk-snooze now that the Tasks entity (PR 2) is on
# ``ee``. Both endpoints delegate per-id to ``ee.cloud.tasks.service`` and
# skip non-Task ids (Instinct Actions don't reassign or snooze). Also
# tagged the per-bulk approve/reject loops with ``# no-event`` comments
# so rule #9 is satisfied without redundant double-emits.
"""Mission Control façade service.

Every function is module-level ``async def`` per ee/cloud rule #5. The
first line of each is ``body = <Request>.model_validate(body)`` (rule #6)
so callers from non-HTTP entry points (CLI, bus handlers, jobs) get the
same validation guarantees as HTTP routes.

Tenancy:
  - Service signature is ``(ctx, body)`` — the workspace lives on
    ``ctx.workspace_id``. We never accept ``workspace_id`` as a
    standalone arg (rule #5).
  - The instinct store is workspace-agnostic at its schema, but we filter
    via the pocket layer: a Nudge surfaces in Mission Control only if
    its pocket is visible to the caller's workspace. The pockets
    service's ``list_pockets`` already enforces this so we can rely on
    it as the chokepoint.

No Beanie writes here — the façade is read-only against Instinct + the
activity buffer. Bulk-approve / bulk-reject delegate to
``ee.instinct.store`` (single ownership of the audit transaction lives
inside Instinct's store). Bulk-reassign / bulk-snooze fan out per-id to
``ee.cloud.tasks.service`` and report which ids weren't Tasks in
``skipped``.

Id conventions inherited from ``_action_to_work_item``: Instinct nudges
project as ``"nudge:<action_id>"``; Tasks project as ``"task:<task_id>"``
(via the Tasks entity's own projector). The bulk endpoints accept either
prefixed or bare ids — anything starting with ``nudge:`` (or any
non-Task prefix) is silently skipped by reassign/snooze because Instinct
Actions don't carry a polymorphic assignee or a due date.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ee.api import get_instinct_store
from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import ValidationError
from ee.cloud.activity.buffer import ActivityEvent, get_buffer
from ee.cloud.mission_control.domain import (
    AssigneeKind,
    WorkItem,
    WorkItemSection,
    WorkItemStatus,
)
from ee.cloud.mission_control.dto import (
    ActivityEventResponse,
    BulkActionRequest,
    BulkReassignRequest,
    BulkSnoozeRequest,
    ListActivityRequest,
    ListWorkItemsRequest,
    OutcomesQueryRequest,
    OutcomeSummaryResponse,
    WorkItemResponse,
    work_item_to_response,
)
from ee.cloud.pockets import service as pockets_service
from ee.instinct.models import Action, ActionStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _require_workspace(ctx: RequestContext) -> str:
    """Refuse to project anything without a workspace.

    Every Mission Control surface is workspace-scoped — there is no
    cross-tenant view by design. A request with ``ctx.workspace_id is
    None`` is a programmer error (probably forgot to set the active
    workspace on the user) and surfaces as 422 instead of silently
    leaking another tenant's data.
    """
    if not ctx.workspace_id:
        raise ValidationError(
            "mission_control.workspace_required",
            "Mission Control requires an active workspace on the request context.",
        )
    return ctx.workspace_id


async def _visible_pocket_ids(ctx: RequestContext, *, project_id: str | None = None) -> set[str]:
    """Return the set of pocket ids the caller can see in their workspace.

    Drives the workspace filter on Instinct reads: a Nudge surfaces in
    Mission Control only if its ``pocket_id`` is in this set. We rely on
    ``pockets_service.list_pockets`` as the chokepoint — it already
    enforces ``workspace + (owner | shared_with | visibility)`` per
    pocket. If a pocket isn't visible at the pocket layer, its Nudges
    aren't visible at the Mission Control layer either.

    ``project_id`` narrows the set to pockets in a single project (or to
    "no project assigned" when an empty string is supplied). Threading
    the filter down here is how Nudges inherit the project assignment
    from their parent pocket — Instinct itself doesn't know about
    projects, but it knows about pockets.
    """
    workspace_id = _require_workspace(ctx)
    pockets = await pockets_service.list_pockets(workspace_id, ctx.user_id, project_id=project_id)
    return {p["_id"] for p in pockets if p.get("_id")}


def _status_to_section_status(s: ActionStatus) -> tuple[WorkItemSection, WorkItemStatus]:
    """Map Instinct ``ActionStatus`` to the (section, status) pair Mission
    Control consumes."""
    if s == ActionStatus.PENDING:
        return WorkItemSection.TRAY, WorkItemStatus.AWAITING_APPROVAL
    if s == ActionStatus.APPROVED:
        return WorkItemSection.PAWPRINTS, WorkItemStatus.APPROVED
    if s == ActionStatus.REJECTED:
        return WorkItemSection.PAWPRINTS, WorkItemStatus.REJECTED
    if s == ActionStatus.EXECUTED:
        return WorkItemSection.PAWPRINTS, WorkItemStatus.DONE
    if s == ActionStatus.FAILED:
        return WorkItemSection.SNAGS, WorkItemStatus.FAILED
    # Defensive — new enum values fall through to the SNAGS pane so they
    # don't disappear from the operator console without explicit handling.
    return WorkItemSection.SNAGS, WorkItemStatus.BLOCKED


def _action_to_work_item(action: Action, workspace_id: str) -> WorkItem:
    """Project an Instinct ``Action`` into a Mission Control ``WorkItem``.

    The assignee field on Instinct is optional — when missing we surface
    the trigger source as the implicit assignee so The Tray still shows
    "who needs to act". This matches the operator mental model better
    than an empty avatar slot.
    """
    section, status = _status_to_section_status(action.status)
    assignee_id = action.assignee or _trigger_assignee(action) or ""
    agent_id = action.trigger.source if action.trigger.type == "agent" else None
    return WorkItem(
        id=f"nudge:{action.id}",
        workspace_id=workspace_id,
        section=section,
        status=status,
        title=action.title,
        description=action.description or action.recommendation or "",
        assignee_kind=AssigneeKind.USER,
        assignee_id=assignee_id,
        pocket_id=action.pocket_id,
        agent_id=agent_id,
        source_kind="nudge",
        source_id=action.id,
        priority=action.priority.value,
        created_at=action.created_at,
        updated_at=action.updated_at,
        fabric_refs=tuple(action.context.object_ids) if action.context else (),
    )


def _trigger_assignee(action: Action) -> str | None:
    """Extract the implicit assignee from the trigger when the explicit
    ``assignee`` column is unset.

    Heuristic: if the trigger is human-sourced (``type='user'``) the
    source IS the assignee — the human routed the work to themselves or
    to a colleague captured in the source. Otherwise we have no signal
    and return None.
    """
    if action.trigger and action.trigger.type == "user":
        return action.trigger.source
    return None


# Status maps for projecting Tasks into the unified WorkItem shape.
_TASK_STATUS_MAP = {
    "proposed": WorkItemStatus.IN_PROGRESS,
    "in_progress": WorkItemStatus.IN_PROGRESS,
    "awaiting_approval": WorkItemStatus.AWAITING_APPROVAL,
    "done": WorkItemStatus.DONE,
    "reverted": WorkItemStatus.REJECTED,
    "failed": WorkItemStatus.FAILED,
    "blocked": WorkItemStatus.BLOCKED,
}


def _task_section(task_status: str, assignee_kind: str) -> WorkItemSection:
    """Bucket a Task into a Mission Control section.

    Agents-in-flight covers any in-progress / proposed agent work.
    Awaiting-approval lands in The Tray regardless of assignee.
    Terminal states route to Pawprints / Snags. Human in-progress falls
    through to TRAY — the frontend's section logic then splits "mine"
    vs "delegated" by comparing the assignee id to the caller.
    """
    if task_status in ("done", "reverted"):
        return WorkItemSection.PAWPRINTS
    if task_status in ("failed", "blocked"):
        return WorkItemSection.SNAGS
    if task_status in ("proposed", "in_progress") and assignee_kind == "agent":
        return WorkItemSection.AGENTS
    return WorkItemSection.TRAY


def _task_to_work_item(task: Any, workspace_id: str) -> WorkItem:
    """Project a ``Task`` (or its DTO) into a Mission Control ``WorkItem``.

    Accepts either a ``tasks.domain.Task`` or a ``TaskResponse`` DTO —
    both expose the same field names so attribute access works on either.
    """
    assignee = task.assignee
    assignee_kind = AssigneeKind.AGENT if assignee.kind == "agent" else AssigneeKind.USER
    status = _TASK_STATUS_MAP.get(task.status, WorkItemStatus.IN_PROGRESS)
    section = _task_section(task.status, assignee.kind)
    return WorkItem(
        id=f"task:{task.id}",
        workspace_id=workspace_id,
        section=section,
        status=status,
        title=task.title,
        description=task.summary or "",
        assignee_kind=assignee_kind,
        assignee_id=assignee.id,
        pocket_id=task.pocket_id or None,
        agent_id=assignee.id if assignee.kind == "agent" else None,
        source_kind="task",
        source_id=task.id,
        priority=task.priority,
        created_at=task.created_at,
        updated_at=task.updated_at,
        fabric_refs=(),
    )


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


async def agent_list_work_items(
    ctx: RequestContext, body: ListWorkItemsRequest | dict[str, Any]
) -> list[WorkItemResponse]:
    """List work items for the active workspace.

    Source-of-truth for PR 1 is Instinct: the pending feed populates The
    Tray, the audit projection populates Pawprints + Snags. PR 2 plugs
    Tasks into the same response so the frontend doesn't have to switch
    code paths when Tasks lands.
    """
    body = ListWorkItemsRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    visible = await _visible_pocket_ids(ctx, project_id=body.project_id)

    items: list[WorkItem] = []

    # --- Instinct Nudges (pocket-scoped) -----------------------------------
    # Nudges always live inside a pocket, so an empty visible set means
    # there are no Nudges to show. Tasks below have their own workspace-
    # level tenancy and are NOT gated by pocket visibility.
    if visible:
        store = get_instinct_store()
        pending = await store.pending(pocket_id=body.pocket)
        resolved = await store.list_actions(pocket_id=body.pocket, limit=200)

        actions: list[Action] = []
        seen: set[str] = set()
        for a in (*pending, *resolved):
            if a.id in seen:
                continue
            if a.pocket_id not in visible:
                continue
            if body.agent and a.trigger.source != body.agent:
                continue
            seen.add(a.id)
            actions.append(a)
        items.extend(_action_to_work_item(a, workspace_id) for a in actions)

    # --- Tasks (workspace-scoped) ------------------------------------------
    # Lazy import keeps the façade installable on forks that haven't
    # adopted the Tasks entity yet (matches the projects/_unassign_project
    # pattern). Tasks live alongside Nudges in the unified feed.
    try:
        from ee.cloud.tasks import service as tasks_service
        from ee.cloud.tasks.dto import ListTasksRequest
    except ImportError:
        logger.info("mission_control.list: tasks entity not installed; skipping")
    else:
        task_req = ListTasksRequest(
            pocket_id=body.pocket,
            project_id=body.project_id,
            limit=200,
        )
        tasks = await tasks_service.agent_list_tasks(ctx, task_req)
        for t in tasks:
            if body.agent and (t.assignee.kind != "agent" or t.assignee.name != body.agent):
                continue
            items.append(_task_to_work_item(t, workspace_id))

    if body.section is not None:
        items = [it for it in items if it.section == body.section]
    # Stable order: newest first by created_at, falling back to id.
    items.sort(key=lambda it: (it.created_at or datetime.min, it.id), reverse=True)
    return [work_item_to_response(it) for it in items[: body.limit]]


async def agent_bulk_approve(
    ctx: RequestContext, body: BulkActionRequest | dict[str, Any]
) -> dict[str, Any]:
    """Approve N pending Nudges in one call.

    Tenancy: each id is checked against the caller's visible-pocket set
    before fanning out to Instinct. Ids that fail that check come back
    in ``missing`` rather than approving across tenants. The shared
    ``bulk_id`` lives in every audit row's ``context.bulk_id`` so the
    operator can recover the bulk transaction.
    """
    body = BulkActionRequest.model_validate(body)
    _require_workspace(ctx)
    visible = await _visible_pocket_ids(ctx)
    store = get_instinct_store()
    eligible, blocked = await _split_ids_by_tenancy(store, list(body.ids), visible)
    approved, missing, bulk_id = await store.bulk_approve(
        eligible, approver=ctx.user_id, note=body.note
    )
    # no-event: per-item approve/reject inside the loop already emits the events
    return {
        "bulk_id": bulk_id,
        "approved": [a.model_dump(mode="json") for a in approved],
        "missing": [*missing, *blocked],
    }


async def agent_bulk_reject(
    ctx: RequestContext, body: BulkActionRequest | dict[str, Any]
) -> dict[str, Any]:
    """Reject N pending Nudges in one call. ``reason`` is required.

    Same tenancy semantics as ``agent_bulk_approve``. The reason text is
    surfaced on every Action's ``rejected_reason`` AND on every audit
    row's ``context.reason`` so the soul-bridge correction pipeline can
    learn from bulk rejects the same way it learns from single-item
    rejects.
    """
    body = BulkActionRequest.model_validate(body)
    if not body.reason:
        raise ValidationError(
            "mission_control.reason_required",
            "bulk-reject requires a reason — pass a non-empty string in ``reason``.",
        )
    _require_workspace(ctx)
    visible = await _visible_pocket_ids(ctx)
    store = get_instinct_store()
    eligible, blocked = await _split_ids_by_tenancy(store, list(body.ids), visible)
    rejected, missing, bulk_id = await store.bulk_reject(
        eligible, reason=body.reason, rejector=ctx.user_id
    )
    # no-event: per-item approve/reject inside the loop already emits the events
    return {
        "bulk_id": bulk_id,
        "rejected": [a.model_dump(mode="json") for a in rejected],
        "missing": [*missing, *blocked],
    }


async def _split_ids_by_tenancy(
    store: Any, ids: list[str], visible_pockets: set[str]
) -> tuple[list[str], list[str]]:
    """Partition ``ids`` into (visible-to-caller, blocked).

    Reads each Action once to look up its pocket. Cheap for the bulk
    sizes Mission Control surfaces (UI selection is bounded by the page
    of items the operator sees). Missing rows fall on the eligible side
    so Instinct's store returns them in its own ``missing`` slot and the
    bulk-action response carries a single deduplicated list.
    """
    eligible: list[str] = []
    blocked: list[str] = []
    for action_id in ids:
        action = await store.get_action(action_id)
        if action is None:
            # Unknown ids stay eligible — Instinct's bulk_* returns them
            # in ``missing`` with no audit side-effect, which is the
            # behavior the operator console expects.
            eligible.append(action_id)
            continue
        if action.pocket_id in visible_pockets:
            eligible.append(action_id)
        else:
            blocked.append(action_id)
    return eligible, blocked


async def agent_outcomes_summary(
    ctx: RequestContext, body: OutcomesQueryRequest | dict[str, Any]
) -> OutcomeSummaryResponse:
    """Aggregate Instinct audit counts over the requested window.

    The window options map to a simple wall-clock cutoff applied in
    Python; there's no Mongo $match $group pipeline because Instinct
    lives on SQLite. For workspaces with millions of audit rows we'd
    push this into a SQL aggregate; the current call volume keeps the
    in-process scan well under the 50ms TimingMiddleware budget.
    """
    body = OutcomesQueryRequest.model_validate(body)
    _require_workspace(ctx)
    visible = await _visible_pocket_ids(ctx)
    store = get_instinct_store()
    cutoff = datetime.now() - _window_to_delta(body.window)

    # Pull a generous slice and filter in Python. ``list_actions`` does
    # ORDER BY created_at DESC LIMIT, so the slice is the newest N.
    actions = await store.list_actions(limit=500)
    in_window = [
        a
        for a in actions
        if a.pocket_id in visible and (a.updated_at or a.created_at or datetime.min) >= cutoff
    ]

    counters: dict[str, int] = {s.value: 0 for s in ActionStatus}
    for a in in_window:
        counters[a.status.value] = counters.get(a.status.value, 0) + 1

    return OutcomeSummaryResponse(
        window=body.window,
        total=len(in_window),
        approved=counters.get(ActionStatus.APPROVED.value, 0),
        rejected=counters.get(ActionStatus.REJECTED.value, 0),
        executed=counters.get(ActionStatus.EXECUTED.value, 0),
        failed=counters.get(ActionStatus.FAILED.value, 0),
        pending=counters.get(ActionStatus.PENDING.value, 0),
    )


def _window_to_delta(window: str) -> timedelta:
    """Map the window string to a timedelta. Validated upstream by the
    DTO regex; defaults to 24h as a safety net."""
    if window == "1h":
        return timedelta(hours=1)
    if window == "24h":
        return timedelta(hours=24)
    if window == "7d":
        return timedelta(days=7)
    return timedelta(hours=24)


async def agent_list_activity(
    ctx: RequestContext, body: ListActivityRequest | dict[str, Any]
) -> list[ActivityEventResponse]:
    """Return the live activity ticker for the active workspace.

    Reads from the in-process buffer (``ee.cloud.activity.buffer``).
    Buffer is bounded + TTL'd so the response is cheap; restarts wipe
    history by design (durable record lives in Pawprints).
    """
    body = ListActivityRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    entries = get_buffer().get_recent(workspace_id, limit=body.limit)
    return [_activity_to_response(e) for e in entries]


def _activity_to_response(e: ActivityEvent) -> ActivityEventResponse:
    return ActivityEventResponse(
        workspace_id=e.workspace_id,
        kind=e.kind,
        agent_id=e.agent_id,
        summary=e.summary,
        pocket_id=e.pocket_id,
        ts=e.ts,
    )


# ---------------------------------------------------------------------------
# Bulk reassign / snooze — fan out to ee.cloud.tasks.service
# ---------------------------------------------------------------------------


def _classify_task_id(raw: str) -> str | None:
    """Pick the Task id out of a Mission Control work-item id, or ``None``
    when the id doesn't refer to a Task.

    The Mission Control wire shape prefixes ids with their source so the
    frontend can render a heterogeneous feed from a single store:
      - ``nudge:<action_id>``  → Instinct action (no reassign, no snooze)
      - ``task:<task_id>``     → Tasks entity
      - bare id                → treated as a Task id for forward
        compatibility with callers that pre-strip the prefix.
    """
    if not raw:
        return None
    if raw.startswith("task:"):
        return raw[len("task:") :] or None
    if ":" in raw:
        # nudge: / cycle: / any other typed prefix — not a Task.
        return None
    return raw


async def agent_bulk_reassign(
    ctx: RequestContext, body: BulkReassignRequest | dict[str, Any]
) -> dict[str, Any]:
    """Reassign N Tasks to the same new assignee in one call.

    Fans out per-id to ``ee.cloud.tasks.service.agent_reassign_task`` so
    each leg lands its own ``task.updated`` event (per-row notifications
    + audit trail stay precise). Ids that don't refer to Tasks land in
    ``skipped`` rather than raising — bulk selections in Mission Control
    routinely mix Nudges and Tasks; the operator's action bar splits
    routing client-side, and the server treats the wrong-kind path
    defensively.
    """
    body = BulkReassignRequest.model_validate(body)
    _require_workspace(ctx)

    from uuid import uuid4

    from ee.cloud.tasks import service as tasks_service
    from ee.cloud.tasks.dto import ReassignTaskRequest

    bulk_id = uuid4().hex
    affected: list[str] = []
    skipped: list[str] = []
    reassign_body = ReassignTaskRequest(
        assignee_kind=body.to.kind,
        assignee_id=body.to.id,
        assignee_name=body.to.name or "",
    )

    for raw_id in body.ids:
        task_id = _classify_task_id(raw_id)
        if task_id is None:
            skipped.append(raw_id)
            continue
        try:
            await tasks_service.agent_reassign_task(ctx, task_id, reassign_body)
            affected.append(raw_id)
        except Exception:
            # NotFound (wrong workspace / missing), Forbidden (caller
            # isn't creator/assignee), or any other Task-level reject —
            # all surface to the operator as "couldn't apply", which is
            # exactly what ``skipped`` represents.
            logger.info(
                "mission_control.bulk_reassign: skipped id %s",
                raw_id,
                exc_info=True,
            )
            skipped.append(raw_id)

    # no-event: per-item agent_reassign_task already emits TaskUpdated per row
    return {"bulk_id": bulk_id, "affected": affected, "skipped": skipped}


async def agent_bulk_snooze(
    ctx: RequestContext, body: BulkSnoozeRequest | dict[str, Any]
) -> dict[str, Any]:
    """Snooze N Tasks to the same ``until_iso`` timestamp in one call.

    Implemented as a partial update on ``due_at`` per task — the Tasks
    entity treats ``due_at`` as the snooze-until column (a Nudge that
    snoozes for an hour is just a Task whose due_at is one hour out).
    Skips ids that aren't Tasks, same semantics as ``agent_bulk_reassign``.
    """
    body = BulkSnoozeRequest.model_validate(body)
    _require_workspace(ctx)

    from uuid import uuid4

    from ee.cloud.tasks import service as tasks_service
    from ee.cloud.tasks.dto import UpdateTaskRequest

    # Parse the ISO timestamp once so an invalid string surfaces as a
    # 422 ValidationError rather than failing per-row inside the loop.
    try:
        until_dt = datetime.fromisoformat(body.until_iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            "mission_control.invalid_until_iso",
            f"until_iso must be an ISO-8601 timestamp; got {body.until_iso!r}",
        ) from exc

    bulk_id = uuid4().hex
    affected: list[str] = []
    skipped: list[str] = []
    update_body = UpdateTaskRequest(due_at=until_dt)

    for raw_id in body.ids:
        task_id = _classify_task_id(raw_id)
        if task_id is None:
            skipped.append(raw_id)
            continue
        try:
            await tasks_service.agent_update_task(ctx, task_id, update_body)
            affected.append(raw_id)
        except Exception:
            logger.info(
                "mission_control.bulk_snooze: skipped id %s",
                raw_id,
                exc_info=True,
            )
            skipped.append(raw_id)

    # no-event: per-item agent_update_task already emits TaskUpdated per row
    return {"bulk_id": bulk_id, "affected": affected, "skipped": skipped}


__all__ = [
    "agent_bulk_approve",
    "agent_bulk_reassign",
    "agent_bulk_reject",
    "agent_bulk_snooze",
    "agent_list_activity",
    "agent_list_work_items",
    "agent_outcomes_summary",
]
