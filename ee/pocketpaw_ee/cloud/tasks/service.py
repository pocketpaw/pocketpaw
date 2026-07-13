# service.py — Tasks entity business logic.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend. Sole
#   owner of writes to the ``Task`` Beanie document. Module-level
#   ``async def`` API per the ee/cloud Code Rules. Emit-on-every-write
#   per Rule 9. Tenant filter on every read per Rule 7. Optimistic
#   single-writer claim via ``find_one_and_update`` so two agents
#   racing on the same proposed task can never both succeed.
# Updated: 2026-05-13 (feat/mission-control-cleanup) — added
#   ``agent_reassign_task_cycle`` so cycle-close rollover can move
#   tasks to the next active cycle (or clear ``cycle_id``) without the
#   creator-or-assignee guard, since workspace admins close cycles
#   they may not own personally.
# Updated: 2026-05-13 (fix/mission-control-followup-nits) — the admin
#   ``agent_reassign_task_cycle`` path now emits a structured audit log
#   line on every call so the creator/assignee-guard bypass is
#   reviewable. PR #1097's reviewer flagged the silent privilege bypass
#   as the highest-priority follow-up.
# Updated: 2026-05-17 (feat/planner-gaps-and-deps) — pocketpaw#1118 P4
#   ``agent_create_task`` persists ``blocked_by`` from the request;
#   ``agent_update_task`` flips it tri-state (None = no change, [] =
#   explicit clear, [...] = replace). Domain mapper threads the field
#   through ``_to_domain`` so projectors (Mission Control's WorkItem)
#   pick it up automatically.
# Updated: 2026-05-21 (feat/taskspec-success-criteria) —
#   ``agent_create_task`` persists ``success_criteria`` and
#   ``preconditions`` from the request; ``_to_domain`` threads them
#   through so completion-time verification (pocketpaw#1162) can read
#   them off the Task.
# Updated: 2026-05-21 (PR #1164 review) — documented in
#   ``agent_update_task`` that success_criteria / preconditions are
#   intentionally not patchable (planner-set, not ad-hoc editable).
# Updated: 2026-07-02 (feat/svl-5-cloud-verify) — ``_to_domain`` threads
#   the new ``verify`` dict (Self-Verifying Loop state stamped by the
#   cloud planner terminal, SVL-5) from the Beanie doc onto the domain
#   Task so the DTO surfaces it read-only. Not patchable through any
#   endpoint — the planner terminal is the sole writer.
"""Tasks entity — business logic service.

Public API (all module-level ``async def``):

  - :func:`agent_create_task` — insert, status defaults by assignee kind.
  - :func:`agent_list_tasks` — workspace-scoped filterable list.
  - :func:`agent_get_task` — single fetch with tenant guard.
  - :func:`agent_update_task` — partial patch of mutable metadata.
  - :func:`agent_claim_task` — atomic ``proposed → in_progress`` flip
    for an agent runtime picking up its queue. First writer wins.
  - :func:`agent_complete_task` — terminal flip to ``done`` or hand-off
    to ``awaiting_approval`` for creator sign-off (the Nudge path).
  - :func:`agent_block_task` — flip to ``blocked`` with a reason so it
    surfaces in the operator's Snags section.
  - :func:`agent_reassign_task` — change the assignee polymorphically.

Every state-mutating function ends with an ``emit(...)`` call on the
realtime bus. Mission Control's frontend subscribes to ``task.*`` events
for live feed updates; the notifications listener subscribes to
``task.proposed`` to create an in-app notification for human assignees.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    TaskBlocked,
    TaskClaimed,
    TaskProposed,
    TaskResolved,
    TaskUpdated,
)
from pocketpaw_ee.cloud.models.task import Task as _TaskDoc
from pocketpaw_ee.cloud.models.task import TaskAssignee as _AssigneeDoc
from pocketpaw_ee.cloud.models.task import TaskSource as _SourceDoc
from pocketpaw_ee.cloud.models.task_event import TaskEvent as _TaskEventDoc
from pocketpaw_ee.cloud.tasks.domain import Task, TaskAssignee, TaskSource
from pocketpaw_ee.cloud.tasks.dto import (
    AttachFilesRequest,
    BlockTaskRequest,
    ClaimTaskRequest,
    CompleteTaskRequest,
    CreateTaskEventRequest,
    CreateTaskRequest,
    ListTasksRequest,
    ReassignTaskRequest,
    TaskAttachmentResponse,
    TaskEventResponse,
    TaskResponse,
    UpdateTaskRequest,
    task_attachment_to_dto,
    task_event_to_dto,
    task_to_dto,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping + access helpers
# ---------------------------------------------------------------------------


def _assignee_to_domain(a: _AssigneeDoc) -> TaskAssignee:
    return TaskAssignee(kind=a.kind, id=a.id, name=a.name)


def _source_to_domain(s: _SourceDoc) -> TaskSource:
    return TaskSource(type=s.type, ref_id=s.ref_id, metadata=dict(s.metadata or {}))


def _to_domain(doc: _TaskDoc) -> Task:
    """Beanie document → frozen domain ``Task``."""

    return Task(
        id=str(doc.id),
        workspace_id=doc.workspace_id,
        creator_id=doc.creator_id,
        assignee=_assignee_to_domain(doc.assignee),
        status=doc.status,  # type: ignore[arg-type]
        priority=doc.priority,  # type: ignore[arg-type]
        kind=doc.kind,  # type: ignore[arg-type]
        source=_source_to_domain(doc.source),
        title=doc.title,
        summary=doc.summary,
        pocket_id=doc.pocket_id,
        cycle_id=doc.cycle_id,
        project_id=getattr(doc, "project_id", None),
        blocked_by=tuple(getattr(doc, "blocked_by", None) or ()),
        success_criteria=tuple(getattr(doc, "success_criteria", None) or ()),
        preconditions=tuple(getattr(doc, "preconditions", None) or ()),
        verify=dict(getattr(doc, "verify", None) or {}),
        due_at=doc.due_at,
        blocked_reason=doc.blocked_reason,
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


async def _fetch_task(ctx: RequestContext, task_id: str) -> _TaskDoc:
    """Load a Task by id, enforce workspace tenancy, raise NotFound on
    miss or on a cross-workspace mismatch. Returning a uniform NotFound
    (not Forbidden) on tenant mismatch prevents callers in another
    workspace from enumerating task ids by 404-vs-403 timing.
    """

    try:
        oid = PydanticObjectId(task_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("task", task_id) from exc
    doc = await _TaskDoc.get(oid)
    if doc is None or doc.workspace_id != ctx.workspace_id:
        raise NotFound("task", task_id)
    return doc


def _event_payload(doc: _TaskDoc, task: Task | None = None) -> dict:
    """Build the realtime event payload for a task mutation.

    The full DTO ships so frontends can render the row without a
    follow-up fetch. ``recipient_ids`` lets the audience resolver fan
    out to the creator and the human assignee (agent assignees don't
    need a WebSocket push — they poll their own queue).
    """

    if task is None:
        task = _to_domain(doc)
    recipients: list[str] = [task.creator_id]
    if task.assignee.kind == "human":
        if task.assignee.id and task.assignee.id != task.creator_id:
            recipients.append(task.assignee.id)
    return {
        "task_id": task.id,
        "task": task_to_dto(task).model_dump(),
        "workspace_id": task.workspace_id,
        "recipient_ids": recipients,
    }


async def _is_workspace_owner(user_id: str, workspace_id: str) -> bool:
    """Check if the user has the ``owner`` role in the given workspace."""
    from pocketpaw_ee.cloud.models.user import User

    user = await User.get(user_id)
    if not user:
        return False
    return any(m.workspace == workspace_id and m.role == "owner" for m in user.workspaces)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def agent_create_task(ctx: RequestContext, body: CreateTaskRequest) -> TaskResponse:
    """Create a Task. Status defaults from ``assignee.kind`` — agent
    assignees land in ``proposed`` (waiting on the claim flow); human
    assignees land in ``in_progress`` (already in someone's hands).

    Workspace tenancy comes from ``ctx``; never accept ``workspace_id``
    as a function parameter (Rule 5).
    """

    body = CreateTaskRequest.model_validate(body)
    if not ctx.workspace_id:
        raise ValidationError("task.no_workspace", "creating a task requires an active workspace")

    status = "proposed" if body.assignee.kind == "agent" else "in_progress"

    if body.project_id:
        await _ensure_project_in_workspace(ctx.workspace_id, body.project_id)

    doc = _TaskDoc(
        workspace_id=ctx.workspace_id,
        creator_id=ctx.user_id,
        pocket_id=body.pocket_id,
        cycle_id=body.cycle_id,
        project_id=body.project_id,
        title=body.title,
        summary=body.summary,
        assignee=_AssigneeDoc(
            kind=body.assignee.kind,
            id=body.assignee.id,
            name=body.assignee.name,
        ),
        assignee_id=body.assignee.id,
        assignee_kind=body.assignee.kind,
        status=status,
        priority=body.priority,
        kind=body.kind,
        source=_SourceDoc(
            type=body.source.type,
            ref_id=body.source.ref_id,
            metadata=dict(body.source.metadata or {}),
        ),
        blocked_by=list(body.blocked_by or []),
        success_criteria=list(body.success_criteria or []),
        preconditions=list(body.preconditions or []),
        due_at=body.due_at,
    )
    await doc.insert()
    task = _to_domain(doc)
    await emit(TaskProposed(data=_event_payload(doc, task)))
    return task_to_dto(task)


async def agent_list_tasks(ctx: RequestContext, body: ListTasksRequest) -> list[TaskResponse]:
    """List tasks visible in the caller's workspace, filtered.

    Mission Control's left rail uses this with ``assignee_id`` to feed
    the per-agent "Agents in flight" sections; the Tray uses it with
    ``status='awaiting_approval'`` plus ``assignee_id=<me>``.
    """

    body = ListTasksRequest.model_validate(body)
    if not ctx.workspace_id:
        return []

    query: dict = {"workspace_id": ctx.workspace_id}
    if body.assignee_id:
        query["assignee_id"] = body.assignee_id
    if body.assignee_kind:
        query["assignee_kind"] = body.assignee_kind
    if body.status:
        query["status"] = body.status
    if body.cycle_id:
        query["cycle_id"] = body.cycle_id
    if body.pocket_id:
        query["pocket_id"] = body.pocket_id
    if body.project_id is not None:
        # Empty string filters for "no project assigned" — the Mission
        # Control "Unassigned" bucket.
        query["project_id"] = body.project_id or None
    if body.creator_id:
        query["creator_id"] = body.creator_id

    docs = (
        await _TaskDoc.find(query)
        .sort(-_TaskDoc.createdAt)  # type: ignore[operator]
        .limit(body.limit)
        .to_list()
    )
    return [task_to_dto(_to_domain(d)) for d in docs]


async def agent_get_task(ctx: RequestContext, task_id: str) -> TaskResponse:
    """Single fetch with tenant check."""

    doc = await _fetch_task(ctx, task_id)
    return task_to_dto(_to_domain(doc))


async def agent_update_task(
    ctx: RequestContext, task_id: str, body: UpdateTaskRequest
) -> TaskResponse:
    """Partial update of mutable Task metadata.

    Status transitions DO NOT come through this endpoint — they use the
    dedicated claim / complete / block / reassign verbs so the audit
    trail and event surface stay precise. Title, summary, priority,
    pocket_id, cycle_id, due_at are fair game here.
    """

    body = UpdateTaskRequest.model_validate(body)
    doc = await _fetch_task(ctx, task_id)
    # No creator/assignee guard here — metadata fields (title, summary,
    # priority, etc.) are safe for any workspace member to edit. Status
    # transitions are protected by their own dedicated endpoints
    # (claim/complete/block/reassign) with tighter guards.

    if body.title is not None:
        doc.title = body.title
    if body.summary is not None:
        doc.summary = body.summary
    if body.priority is not None:
        doc.priority = body.priority
    if body.pocket_id is not None:
        doc.pocket_id = body.pocket_id
    if body.cycle_id is not None:
        doc.cycle_id = body.cycle_id
    if body.project_id is not None:
        if body.project_id:
            await _ensure_project_in_workspace(doc.workspace_id, body.project_id)
            doc.project_id = body.project_id
        else:
            doc.project_id = None
    if body.blocked_by is not None:
        # Tri-state: None = no change (handled by the outer guard),
        # [] = explicit clear, [...] = replace the full set. The Beanie
        # field defaults to ``[]`` for old docs so the assignment is
        # always safe.
        doc.blocked_by = list(body.blocked_by)
    if "due_at" in body.model_fields_set:
        # ``model_fields_set`` distinguishes explicit ``null`` (clears the
        # field) from "not provided" (leaves the current value untouched).
        doc.due_at = body.due_at

    # success_criteria / preconditions are deliberately NOT patchable
    # here: they are planner-set at materialization time (the verifiable
    # contract for the task) and should not drift via ad-hoc edits.
    # UpdateTaskRequest omits them on purpose — not an oversight.

    await doc.save()
    task = _to_domain(doc)
    await emit(TaskUpdated(data=_event_payload(doc, task)))
    return task_to_dto(task)


async def agent_set_task_cycle(
    ctx: RequestContext, task_id: str, cycle_id: str | None
) -> TaskResponse:
    """Attach (or detach when ``cycle_id`` is ``None``) a task to a sprint.

    Project-management primitive used by the Mission Control facade for the
    "+ existing" attach flow. Unlike :func:`agent_update_task` this does NOT
    enforce the creator/assignee gate — any caller with workspace access can
    set the cycle pointer, which is the right posture for sprint planning
    (the sprint owner is typically not the task's creator or assignee).
    Workspace tenancy is still enforced via ``_fetch_task``.

    Audit: emits a structured ``tasks.set_cycle`` log line on every call so
    the creator/assignee-guard bypass is reviewable, matching the precedent
    set by :func:`agent_reassign_task_cycle` after PR #1097's review. The
    distinct log key (``set_cycle`` vs ``reassign_cycle``) lets audit queries
    separate the sprint-planning attach flow from the cycle-rollover flow.
    """

    doc = await _fetch_task(ctx, task_id)
    previous_cycle_id = doc.cycle_id
    doc.cycle_id = cycle_id
    await doc.save()
    logger.info(
        "tasks.set_cycle workspace=%s caller=%s task=%s from=%s to=%s",
        ctx.workspace_id,
        ctx.user_id,
        task_id,
        previous_cycle_id,
        cycle_id,
    )
    task = _to_domain(doc)
    await emit(TaskUpdated(data=_event_payload(doc, task)))
    return task_to_dto(task)


# ---------------------------------------------------------------------------
# State-machine verbs
# ---------------------------------------------------------------------------


async def agent_claim_task(ctx: RequestContext, task_id: str, body: ClaimTaskRequest) -> dict:
    """Optimistic single-writer claim.

    Returns ``{"ok": True, "task": <TaskResponse dict>}`` when the
    caller successfully claimed the task, or
    ``{"ok": False, "reason": "<code>"}`` when the row was already
    claimed by someone else, doesn't exist, or doesn't belong to this
    agent.

    The atomic move lives in a single Mongo ``find_one_and_update`` on
    ``{_id, workspace_id, status: 'proposed', assignee.id: agent_id}``.
    If two agent runtimes race on the same proposed task, exactly one
    update matches; the second sees ``None`` and returns
    ``{ok: False, reason: 'already_claimed'}``. No transaction needed.
    """

    body = ClaimTaskRequest.model_validate(body)
    if not ctx.workspace_id:
        return {"ok": False, "reason": "no_workspace"}

    try:
        oid = PydanticObjectId(task_id)
    except Exception:
        return {"ok": False, "reason": "not_found"}

    now = datetime.now(UTC)
    collection = _TaskDoc.get_pymongo_collection()
    updated = await collection.find_one_and_update(
        {
            "_id": oid,
            "workspace_id": ctx.workspace_id,
            "status": "proposed",
            "assignee_id": body.agent_id,
            "assignee_kind": "agent",
        },
        {"$set": {"status": "in_progress", "updatedAt": now}},
        return_document=True,  # mongomock + pymongo accept bool: True == AFTER
    )
    if updated is None:
        # Disambiguate: does the task even exist for this workspace?
        existing = await collection.find_one(
            {"_id": oid, "workspace_id": ctx.workspace_id},
            projection={"status": 1, "assignee_id": 1, "assignee_kind": 1},
        )
        if existing is None:
            return {"ok": False, "reason": "not_found"}
        if existing.get("assignee_id") != body.agent_id:
            return {"ok": False, "reason": "not_assigned_to_agent"}
        if existing.get("status") != "proposed":
            return {"ok": False, "reason": "already_claimed"}
        return {"ok": False, "reason": "claim_failed"}

    doc = await _TaskDoc.get(oid)
    if doc is None:
        # Shouldn't happen — we just updated it — but defensive.
        return {"ok": False, "reason": "race"}
    task = _to_domain(doc)
    await emit(TaskClaimed(data=_event_payload(doc, task)))
    return {"ok": True, "task": task_to_dto(task).model_dump()}


async def agent_complete_task(
    ctx: RequestContext, task_id: str, body: CompleteTaskRequest
) -> TaskResponse:
    """Finish work on a task.

    ``next_action='archive'`` → status flips to ``done`` and the row
    leaves Mission Control's active feed.

    ``next_action='request_approval'`` → status flips to
    ``awaiting_approval`` and the row reappears in the creator's Tray
    as a Nudge for sign-off. This is the path agent runtimes take after
    drafting work product the human still wants to review.
    """

    body = CompleteTaskRequest.model_validate(body)
    doc = await _fetch_task(ctx, task_id)

    if doc.creator_id != ctx.user_id and doc.assignee_id != ctx.user_id:
        # Workspace owners can also complete tasks (bulk approve from MC).
        if not await _is_workspace_owner(ctx.user_id, doc.workspace_id):
            raise Forbidden(
                "task.complete_denied",
                "Only the creator, assignee, or workspace owner can complete this task",
            )

    if doc.status in {"done", "reverted", "failed"}:
        raise ValidationError("task.terminal", f"task is already {doc.status!r}")

    new_status = "done" if body.next_action == "archive" else "awaiting_approval"
    doc.status = new_status
    if body.result_summary:
        # Append a result line to the summary so the detail panel shows
        # the agent's hand-off note without a separate field migration.
        if doc.summary:
            doc.summary = (doc.summary + "\n\n" + body.result_summary).strip()
        else:
            doc.summary = body.result_summary
    await doc.save()
    task = _to_domain(doc)
    await emit(TaskResolved(data=_event_payload(doc, task)))
    return task_to_dto(task)


async def agent_block_task(
    ctx: RequestContext, task_id: str, body: BlockTaskRequest
) -> TaskResponse:
    """Mark the task blocked with a reason. Surfaces in the operator's
    Snags section. Recoverable: a subsequent ``agent_update_task`` or
    ``agent_claim_task`` can move it back to ``in_progress``."""

    body = BlockTaskRequest.model_validate(body)
    doc = await _fetch_task(ctx, task_id)
    if doc.creator_id != ctx.user_id and doc.assignee_id != ctx.user_id:
        # Workspace owners can also block tasks (bulk reject from MC feed).
        if not await _is_workspace_owner(ctx.user_id, doc.workspace_id):
            raise Forbidden("task.block_denied", "Only the creator or assignee can block this task")
    doc.status = "blocked"
    doc.blocked_reason = body.reason
    await doc.save()
    task = _to_domain(doc)
    await emit(TaskBlocked(data=_event_payload(doc, task)))
    return task_to_dto(task)


async def agent_revert_task(ctx: RequestContext, task_id: str) -> TaskResponse:
    """Revert a task from a terminal status back to in_progress.

    Moves a task from ``done``, ``reverted``, or ``failed`` back to
    ``in_progress`` so work can resume. This is the inverse of
    ``agent_complete_task`` — it un-marks finished work.

    Raises:
        ValidationError: If the task is not in a revertable state.
        NotFound: If the task does not exist in the caller's workspace.
        Forbidden: If the caller is not authorized.
    """
    doc = await _fetch_task(ctx, task_id)

    if doc.status not in ("done", "reverted", "failed"):
        raise ValidationError(
            "task.not_revertable",
            f"Cannot revert task with status {doc.status!r}. "
            "Only done, reverted, or failed tasks can be reverted.",
        )

    doc.status = "in_progress"
    await doc.save()
    task = _to_domain(doc)
    await emit(TaskUpdated(data=_event_payload(doc, task)))
    return task_to_dto(task)


async def agent_reassign_task(
    ctx: RequestContext, task_id: str, body: ReassignTaskRequest
) -> TaskResponse:
    """Change the task's assignee. Kind may flip (human → agent or
    vice versa). If the task was ``proposed`` and the new assignee is a
    human, we leave it ``proposed`` so the human sees it as pending; if
    the new assignee is an agent, also ``proposed`` so the claim path
    fires. Other statuses pass through.
    """

    body = ReassignTaskRequest.model_validate(body)
    doc = await _fetch_task(ctx, task_id)
    if doc.creator_id != ctx.user_id and doc.assignee_id != ctx.user_id:
        # Workspace owners can also reassign tasks (bulk reassign from MC,
        # picking up unassigned meeting-notes action items, etc.).
        if not await _is_workspace_owner(ctx.user_id, doc.workspace_id):
            raise Forbidden(
                "task.reassign_denied",
                "Only the creator, assignee, or workspace owner can reassign this task",
            )
    doc.assignee = _AssigneeDoc(
        kind=body.assignee_kind,
        id=body.assignee_id,
        name=body.assignee_name,
    )
    doc.assignee_id = body.assignee_id
    doc.assignee_kind = body.assignee_kind
    await doc.save()
    task = _to_domain(doc)
    await emit(TaskUpdated(data=_event_payload(doc, task)))
    return task_to_dto(task)


# ---------------------------------------------------------------------------
# Agent-runtime helpers
# ---------------------------------------------------------------------------


async def agent_reassign_task_cycle(
    ctx: RequestContext, task_id: str, new_cycle_id: str | None
) -> TaskResponse:
    """Move a Task to a different cycle (or clear its cycle).

    Used by ``cycles.service._roll_incomplete_tasks`` when a cycle
    closes — incomplete tasks roll to the next active cycle on the same
    pocket, and ``new_cycle_id=None`` drops them back into the
    unscheduled list. Workspace-admin pathway: skips the per-row
    creator/assignee guard that ``agent_update_task`` enforces, because
    the cycle owner (not the task owner) is the one closing the cycle.
    Still tenant-scoped via ``_fetch_task``.

    Audit: emits a structured ``tasks.reassign_cycle`` log line on every
    call (caller, task, from→to cycle) so the privilege bypass is
    reviewable. The line is INFO-level — the operation is expected on
    cycle close — but carries enough context for after-the-fact audit.
    """

    doc = await _fetch_task(ctx, task_id)
    previous_cycle_id = doc.cycle_id
    doc.cycle_id = new_cycle_id
    await doc.save()
    logger.info(
        "tasks.reassign_cycle workspace=%s caller=%s task=%s from=%s to=%s",
        ctx.workspace_id,
        ctx.user_id,
        task_id,
        previous_cycle_id,
        new_cycle_id,
    )
    task = _to_domain(doc)
    await emit(TaskUpdated(data=_event_payload(doc, task)))
    return task_to_dto(task)


async def list_for_agent_runtime(
    workspace_id: str, agent_id: str, status: str = "proposed", limit: int = 50
) -> list[TaskResponse]:
    """Return tasks assigned to a specific agent.

    Used by the in-process MCP ``list_my_tasks`` tool which doesn't have
    a RequestContext (it runs inside the agent process, identified by
    per-stream ContextVars). The function takes ``workspace_id`` /
    ``agent_id`` directly and applies the standard tenant filter inline.
    """

    if not workspace_id or not agent_id:
        return []
    query: dict = {
        "workspace_id": workspace_id,
        "assignee_id": agent_id,
        "assignee_kind": "agent",
    }
    if status:
        query["status"] = status
    docs = (
        await _TaskDoc.find(query)
        .sort(-_TaskDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
        .to_list()
    )
    return [task_to_dto(_to_domain(d)) for d in docs]


async def _ensure_project_in_workspace(workspace_id: str, project_id: str) -> None:
    """Validate that ``project_id`` exists in the workspace. Lazy-imports
    the projects service to avoid circular imports at module load and to
    degrade silently on forks that predate the Projects entity.
    """
    try:
        from pocketpaw_ee.cloud.projects import service as projects_service
    except Exception:
        return
    ok = await projects_service.exists_in_workspace(workspace_id, project_id)
    if not ok:
        from pocketpaw_ee.cloud._core.errors import NotFound as _NotFound

        raise _NotFound("project", project_id)


async def unassign_project_on_tasks(workspace_id: str, project_id: str) -> int:
    """Soft-unassign every task in ``workspace_id`` whose ``project_id``
    matches. Called by ``projects.service.agent_delete`` when a project
    is removed — tasks keep their data, only the project reference
    clears. Returns the number of rows updated.

    Stays inside the tasks service so the 4-file rule holds (only
    ``tasks/service.py`` may write to the Task Beanie collection).
    """
    if not workspace_id or not project_id:
        return 0
    collection = _TaskDoc.get_pymongo_collection()
    result = await collection.update_many(
        {"workspace_id": workspace_id, "project_id": project_id},
        {"$set": {"project_id": None}},
    )
    return getattr(result, "modified_count", 0) or 0


# ---------------------------------------------------------------------------
# Attachments — link uploaded files to a task
# ---------------------------------------------------------------------------


async def agent_attach_files(
    ctx: RequestContext, task_id: str, body: AttachFilesRequest
) -> list[TaskAttachmentResponse]:
    """Link one or more previously-uploaded files to a task.

    Accepts a list of ``file_ids`` returned from ``POST /api/v1/uploads``.
    Each ``file_id`` is resolved against the workspace-scoped uploads
    service to verify it exists and belongs to the caller's workspace.
    Skips ids that are already linked to this task (idempotent).

    Returns the list of ``TaskAttachmentResponse`` DTOs for the newly
    linked files.
    """
    from pocketpaw_ee.cloud.models.task_attachment import TaskAttachment as _AttachmentDoc
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    body = AttachFilesRequest.model_validate(body)
    await _fetch_task(ctx, task_id)  # tenant guard

    # Resolve each file_id against the workspace-scoped uploads store
    file_store = MongoFileStore()
    attached: list[TaskAttachmentResponse] = []
    for file_id in body.file_ids:
        file_doc = await file_store.get_doc_scoped(file_id, workspace=ctx.workspace_id)
        if file_doc is None:
            continue
        # Check if already linked
        existing = await _AttachmentDoc.find_one(
            _AttachmentDoc.task_id == task_id,
            _AttachmentDoc.file_id == file_id,
        )
        if existing is not None:
            continue
        att_doc = _AttachmentDoc(
            task_id=task_id,
            workspace_id=ctx.workspace_id,
            creator_id=ctx.user_id,
            file_id=file_id,
            filename=file_doc.filename,
            mime=file_doc.mime,
            size=file_doc.size,
        )
        await att_doc.insert()
        attached.append(task_attachment_to_dto(att_doc))

    return attached


async def agent_list_attachments(ctx: RequestContext, task_id: str) -> list[TaskAttachmentResponse]:
    """List all files attached to a task."""
    from pocketpaw_ee.cloud.models.task_attachment import TaskAttachment as _AttachmentDoc

    _ = await _fetch_task(ctx, task_id)  # tenant guard
    docs = (
        await _AttachmentDoc.find(_AttachmentDoc.task_id == task_id)
        .sort(-_AttachmentDoc.createdAt)  # type: ignore[operator]
        .to_list()
    )
    return [task_attachment_to_dto(d) for d in docs]


async def agent_delete_attachment(ctx: RequestContext, task_id: str, attachment_id: str) -> None:
    """Remove a file attachment from a task.

    Only the creator of the attachment or a workspace owner may delete it.
    The underlying uploaded file is NOT deleted — only the task link.
    """
    from pocketpaw_ee.cloud.models.task_attachment import TaskAttachment as _AttachmentDoc

    _ = await _fetch_task(ctx, task_id)  # tenant guard
    att = await _AttachmentDoc.get(attachment_id)
    if att is None or att.task_id != task_id or att.workspace_id != ctx.workspace_id:
        raise NotFound("attachment", attachment_id)
    if att.creator_id != ctx.user_id and not await _is_workspace_owner(
        ctx.user_id, ctx.workspace_id
    ):
        raise Forbidden(
            "attachment.delete_denied",
            "Only the creator or workspace owner can delete attachments",
        )
    await att.delete()


# ---------------------------------------------------------------------------
# Task Event (comments/activity on a task)
# ---------------------------------------------------------------------------


async def agent_create_task_event(
    ctx: RequestContext, task_id: str, body: CreateTaskEventRequest
) -> TaskEventResponse:
    """Post a comment/activity entry on a task."""
    body = CreateTaskEventRequest.model_validate(body)
    task_doc = await _fetch_task(ctx, task_id)
    from pocketpaw_ee.cloud.models.user import User

    user = await User.get(ctx.user_id)
    author_name = user.full_name if user else ctx.user_id
    doc = _TaskEventDoc(
        workspace_id=task_doc.workspace_id,
        task_id=task_id,
        author_id=ctx.user_id,
        author_name=author_name,
        body=body.body,
    )
    await doc.insert()
    # Emit a bus event so the frontend gets a realtime update
    event_dto = task_event_to_dto(doc)
    await emit(
        TaskUpdated(
            data={
                "task_id": task_id,
                "task": None,
                "event": event_dto.model_dump(mode="json"),
                "workspace_id": task_doc.workspace_id,
                "recipient_ids": [task_doc.creator_id],
            }
        )
    )
    return event_dto


async def agent_list_task_events(
    ctx: RequestContext, task_id: str, limit: int = 50
) -> list[TaskEventResponse]:
    """List events for a task, newest first."""
    _ = await _fetch_task(ctx, task_id)  # tenant guard
    docs = (
        await _TaskEventDoc.find(
            _TaskEventDoc.task_id == task_id,
            _TaskEventDoc.workspace_id == ctx.workspace_id,
        )
        .sort(-_TaskEventDoc.createdAt)  # type: ignore[operator]
        .limit(max(1, min(limit, 200)))
        .to_list()
    )
    return [task_event_to_dto(d) for d in docs]


__all__ = [
    "agent_attach_files",
    "agent_block_task",
    "agent_claim_task",
    "agent_complete_task",
    "agent_create_task",
    "agent_create_task_event",
    "agent_delete_attachment",
    "agent_get_task",
    "agent_list_attachments",
    "agent_list_task_events",
    "agent_list_tasks",
    "agent_reassign_task",
    "agent_reassign_task_cycle",
    "agent_revert_task",
    "agent_set_task_cycle",
    "agent_update_task",
    "list_for_agent_runtime",
    "unassign_project_on_tasks",
]
