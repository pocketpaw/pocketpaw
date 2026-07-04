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
# Updated: 2026-05-17 (feat/planner-gaps-and-deps) — pocketpaw#1118 P4
# threaded ``task.blocked_by`` through the WorkItem projection. Each
# dependency id is prefixed with ``task:`` so the frontend's heterogeneous
# feed can link dependency edges to their corresponding WorkItem rows
# without translating ids client-side.
# Updated: 2026-05-18 (feat/mc-plan-sessions-endpoint) — added
# ``agent_list_plan_sessions`` that delegates to
# ``planner.service.list_plan_sessions`` (the only module allowed to
# touch the PlanSession Beanie doc) and DTO-maps the typed summaries to
# the wire envelope. Status vocabulary mapping (ready/stale ↔
# draft/archived) lives here so the planner entity keeps its internal
# vocabulary while Mission Control surfaces the operator's terms.
# Updated: 2026-05-19 (feat/mc-create-cycle-endpoint) — added
# ``agent_create_cycle`` that backs POST /mission-control/cycles for the
# rail's "+ New cycle" button. Parses the wire's ISO strings, derives
# ``status`` from start/end relative to ``now`` (upcoming | active), and
# delegates the actual Beanie write to ``cycles.service.agent_create_cycle``
# — the single-owner rule (only ``ee.cloud.cycles.service`` may write to
# the Cycle Beanie doc) is enforced by an import-linter forbidden
# contract; the MC façade can never bypass it.
# Updated: 2026-06-10 (W4c — scope instinct reads to workspace) — closes the
# residual cross-tenant read surface W4a left open on the INTERNAL caller side.
# W4a workspace-scoped the public instinct router endpoints + the store reads,
# but this façade still called the (shared, global) instinct store WITHOUT a
# ``workspace_id``, so on shared infra it read every tenant's Nudges/audit
# before the pocket-visibility filter ran (and ``agent_outcomes_summary`` is
# NOT pocket-filtered at the store, only in Python). The three instinct reads
# — ``store.pending`` / ``store.list_actions`` in ``agent_list_work_items`` and
# ``store.list_actions`` in ``agent_outcomes_summary`` — now thread the caller's
# ``ctx.workspace_id`` (already resolved by ``_require_workspace``) into the
# store so the SQL restricts to the tenant's own rows (plus legacy NULL rows)
# BEFORE the existing pocket filter. ``workspace_id`` crosses to the OSS store
# as a PLAIN str; this is a read FILTER only and never touches the W2b audit
# hash chain. The bulk-approve / bulk-reject WRITE paths already gate on
# ``_visible_pocket_ids`` (pocket-layer tenancy), so they are unchanged.
# Updated: 2026-06-10 (fix/mc-bulk-approve-strands-writes — W0c) — fixed
# two defects in ``agent_bulk_approve`` that together stranded every write
# a manager bulk-approved, undermining the Instinct governance moat:
#   (1) Routing regression — a prior commit routed ids through
#       ``_classify_task_id``, whose forward-compat default classifies a
#       BARE id as a Task. Bulk-approving a Nudge by its bare id (the shape
#       the frontend + every existing test send) was misrouted to the
#       Tasks branch, failed ``agent_complete_task``, and landed in
#       ``missing`` — the Nudge was never even approved. Routing is now by
#       explicit prefix (``task:`` → Tasks; ``nudge:`` / bare → Instinct
#       Nudge, prefix stripped), consistent with ``agent_bulk_reject``.
#   (2) Stranded writes — even once a Nudge approved, the façade called
#       ``store.bulk_approve`` and stopped, never firing the parked
#       ``_pocket_write``, so the write stalled at ``approved`` forever
#       (no execution, no audit/chain emit). The façade now mirrors the
#       single-/bulk-approve HTTP path
#       (``ee.instinct.router.bulk_approve_actions``): for each approved
#       Nudge it emits ``human.corrected(accepted)`` +
#       ``policy.evaluated(passed=True)`` and fires
#       ``instinct_bridge.execute_approved_write`` (which lands the write
#       and closes the chain), reusing the router's helpers rather than
#       forking the chain logic. New ``_execute_bulk_approved_nudge`` runs
#       one item with per-item error isolation; the response carries an
#       ``executed`` list of per-item outcomes so one failing write can't
#       silently drop the rest.
# Updated: 2026-06-10 (sov/r2a FIX 3) — ``_execute_bulk_approved_nudge`` now
# imports the chain-emit helpers (``_emit_human_corrected`` /
# ``_emit_policy_evaluated_approved`` / ``_pocket_write_blob``) from the new
# shared ``ee.instinct.chain_emitters`` module instead of reaching into the
# Instinct router's private internals. Behavior is identical (same helpers,
# same call order) — the façade no longer couples to the router.
# Updated: 2026-06-12 (fix/tray-workspace-scoped-nudges) — workspace-scoped
# nudges now reach The Tray. External-action proposals stamp
# ``Action.pocket_id = workspace_id`` (they aren't pocket-bound — see
# ``external_actions/propose.py``), but ``agent_list_work_items`` dropped any
# action whose ``pocket_id`` wasn't a visible POCKET id, so every gated
# external action sat pending without ever surfacing in the feed. The
# per-action filter now also admits ``a.pocket_id == workspace_id`` (the
# caller's own workspace only — the W4c store-level scope still keeps other
# tenants' rows out in SQL), the instinct block runs even when the workspace
# has zero visible pockets, and a workspace-scoped item projects with
# pocket_name "Workspace" instead of leaking the raw workspace hex id.
# Pocket-bound nudges keep the exact same visibility filter as before.
# Updated: 2026-07-04 (fix/approval-resolution) — closed the list-vs-approve
# tenancy mismatch that made a workspace-scoped nudge (``pocket_id ==
# workspace_id`` — ``_admin_action`` / ``_external_action`` proposals) LIST in
# The Tray but report ``missing`` on bulk-approve, leaving the proposal pending
# forever. TWO fixes: (1) ``_split_ids_by_tenancy`` now admits ``pocket_id ==
# workspace_id`` — the SAME clause ``agent_list_work_items`` uses — so a nudge
# that lists also resolves; the store read is already tenant-scoped, so this
# only ever admits the caller's own workspace. (2) ``_execute_bulk_approved_nudge``
# now dispatches EVERY gated blob kind's executor (new
# ``_execute_gated_bulk_approved_nudge`` mirrors the router's per-kind dispatch
# for ``_admin_action`` / ``_external_action`` / ``_code_change`` /
# ``_fabric_objects`` / ``_pocket_create`` / ``_instinct_rule`` / ``_belt_plan``
# / ``_artifact_change``), not just ``_pocket_write`` — so a bulk-approved admin
# action actually EXECUTES (e.g. ``billing.manage`` produces its Dodo checkout
# url) instead of flipping to ``approved`` and stranding the write. The router's
# approve/executor logic is unchanged; the façade reuses its blob accessors +
# executors.
# Updated: 2026-07-04 (fix/approval-resolution) — two projection fixes so The
# Tray shows human names and honors its status filter:
#   (1) Actor NAME resolution — a gated proposal's ``trigger.source`` is the
#       PROPOSER user id (a raw ObjectId hex — see ``admin_proposals/propose.py``
#       and ``external_actions/propose.py``, both ``trigger.type == "agent"``).
#       ``_action_to_work_item`` projected that raw id into ``agent_name`` /
#       ``assignee_name``, so the approval tray rendered ``6a47…`` instead of a
#       person. ``agent_list_work_items`` now collects the unique trigger.source
#       ids across all projected actions and batch-resolves them ONCE via a
#       single ``_UserDoc.find({"_id": {"$in": ...}})`` (the ripple_sources /
#       group_service pattern, mirroring how ``_pocket_name_map`` builds its map
#       once), then passes an ``actor_name_map`` into ``_action_to_work_item``.
#       Name preference: ``full_name`` → ``email`` → the id (never raises; a
#       malformed / non-ObjectId source or an unknown user falls back to the id).
#   (2) ``status`` filter — the ``/items`` endpoint accepted ``section`` but not
#       the ``status`` it documents, so ``GET /items?status=pending`` was
#       silently ignored and terminal (done/failed) items leaked into the
#       awaiting-approval feed. ``agent_list_work_items`` now filters the
#       assembled items by ``body.status`` when set, with ``"pending"`` aliased
#       to ``WorkItemStatus.AWAITING_APPROVAL`` (the projection already maps
#       ``ActionStatus.PENDING`` → that). ``status=None`` returns everything as
#       before; ``status`` composes with the section/agent/pocket filters.
"""Mission Control façade service.

Every function is module-level ``async def`` per ee/cloud rule #5. The
first line of each is ``body = <Request>.model_validate(body)`` (rule #6)
so callers from non-HTTP entry points (CLI, bus handlers, jobs) get the
same validation guarantees as HTTP routes.

Tenancy:
  - Service signature is ``(ctx, body)`` — the workspace lives on
    ``ctx.workspace_id``. We never accept ``workspace_id`` as a
    standalone arg (rule #5).
  - Two layers of tenancy, both scoped to ``ctx.workspace_id``:
      1. Store-level (W4c): every instinct read threads the caller's
         ``workspace_id`` into the store, so the global shared SQLite DB
         restricts rows to this tenant (plus legacy NULL rows) in SQL —
         before anything reaches Python. This closes the cross-tenant
         leak on shared infra; it is the load-bearing isolation.
      2. Pocket-level: a Nudge additionally only surfaces if its pocket
         is visible to the caller's workspace. ``pockets_service.list_pockets``
         enforces this as the chokepoint. This stays as a second filter
         (owner / shared_with / visibility within the tenant), layered on
         top of the store scope, not in place of it. Workspace-scoped
         nudges (``pocket_id == workspace_id``, e.g. external-action
         proposals) bypass only this pocket layer — the store scope in
         (1) is still what isolates them per tenant.

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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pocketpaw.instinct.models import Action, ActionStatus
from pocketpaw_ee.api import get_instinct_store
from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.activity.buffer import ActivityEvent, get_buffer
from pocketpaw_ee.cloud.mission_control.domain import (
    AssigneeKind,
    WorkItem,
    WorkItemSection,
    WorkItemStatus,
)
from pocketpaw_ee.cloud.mission_control.dto import (
    ActivityEventResponse,
    AnalyticsAgentDTO,
    AnalyticsDayDTO,
    AnalyticsPocketDTO,
    AnalyticsResponse,
    AttachCycleItemsRequest,
    AttachCycleItemsResponse,
    BulkActionRequest,
    BulkReassignRequest,
    BulkRevertRequest,
    BulkSnoozeRequest,
    CreateCycleRequest,
    DetachCycleItemsResponse,
    ListActivityRequest,
    ListPlanSessionsRequest,
    ListWorkItemsRequest,
    OutcomesQueryRequest,
    OutcomeSummaryResponse,
    PlanSessionDTO,
    PlanSessionListResponse,
    WorkItemResponse,
    work_item_to_response,
)
from pocketpaw_ee.cloud.pockets import service as pockets_service

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


def _record_deep_work_audit(
    workspace_id: str,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget audit recording for deep work operations.

    Never raises — failures are logged and swallowed so an audit outage
    cannot block a legitimate operation.
    """
    import asyncio

    from pocketpaw_ee.cloud.deep_work_log import service as _dw_log_service

    asyncio.ensure_future(
        _dw_log_service.record(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata or {},
        )
    )


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


async def _pocket_name_map(ctx: RequestContext, *, project_id: str | None = None) -> dict[str, str]:
    """Build pocket_id → pocket_name mapping for visible pockets."""
    workspace_id = _require_workspace(ctx)
    pockets = await pockets_service.list_pockets(workspace_id, ctx.user_id, project_id=project_id)
    return {p["_id"]: p.get("name", p["_id"]) for p in pockets if p.get("_id")}


async def _resolve_actor_names(source_ids: set[str]) -> dict[str, str]:
    """Batch-resolve a set of trigger.source user ids → display names.

    A gated proposal's ``trigger.source`` is the proposer user id (a raw
    ObjectId hex — see ``admin_proposals/propose.py`` /
    ``external_actions/propose.py``). This maps each to a human display
    name so The Tray never renders the raw id.

    Built ONCE per ``agent_list_work_items`` call from the union of all
    projected actions' sources — a single ``_UserDoc.find`` over the id
    set, mirroring how ``_pocket_name_map`` resolves pocket names once
    (and the ``ripple_sources`` / ``chat.group_service`` batch pattern).

    Name preference: ``full_name`` → ``email`` → the id. Never raises: a
    non-ObjectId / malformed source is skipped (stays the id via the
    caller's ``.get(source, source)`` fallback), and an id with no
    matching user simply isn't in the returned map (same fallback). We
    resolve names across the workspace's user set, not just members, so a
    proposer who has since left still renders as a name.
    """
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User as _UserDoc

    object_ids: list[PydanticObjectId] = []
    for sid in source_ids:
        try:
            object_ids.append(PydanticObjectId(sid))
        except Exception:
            # Non-ObjectId source (e.g. an agent name, or a sentinel like
            # "external_action" / "admin_action") — leave it as the id.
            logger.debug("mission_control: skipping non-ObjectId trigger source %r", sid)
    if not object_ids:
        return {}

    users = await _UserDoc.find({"_id": {"$in": object_ids}}).to_list()
    return {
        str(u.id): ((u.full_name or "").strip() or (u.email or "").strip() or str(u.id))
        for u in users
    }


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


def _action_to_work_item(
    action: Action,
    workspace_id: str,
    pocket_name: str = "",
    actor_name_map: dict[str, str] | None = None,
) -> WorkItem:
    """Project an Instinct ``Action`` into a Mission Control ``WorkItem``.

    The assignee field on Instinct is optional — when missing we surface
    the trigger source as the implicit assignee so The Tray still shows
    "who needs to act". This matches the operator mental model better
    than an empty avatar slot.

    ``actor_name_map`` maps a ``trigger.source`` user id → the user's
    display name (built once by ``agent_list_work_items`` — see
    ``_resolve_actor_names``). For a gated proposal ``trigger.source`` is
    the PROPOSER user id (a raw ObjectId hex), so we render the name
    instead of leaking the id into ``agent_name`` / ``assignee_name``.
    When the source isn't in the map (unknown user, malformed id) we fall
    back to the id — the same non-leaking behavior the map's builder uses.
    """
    name_map = actor_name_map or {}
    section, status = _status_to_section_status(action.status)
    assignee_id = action.assignee or _trigger_assignee(action) or ""
    agent_id = action.trigger.source if action.trigger.type == "agent" else None
    agent_name = name_map.get(agent_id, agent_id) if agent_id else ""
    assignee_name_raw = _trigger_assignee_name(action) or assignee_id
    assignee_name = name_map.get(assignee_name_raw, assignee_name_raw)
    return WorkItem(
        id=f"nudge:{action.id}",
        workspace_id=workspace_id,
        section=section,
        status=status,
        title=action.title,
        description=action.description or action.recommendation or "",
        assignee_kind=AssigneeKind.USER,
        assignee_id=assignee_id,
        assignee_name=assignee_name,
        agent_id=agent_id,
        agent_name=agent_name,
        pocket_id=action.pocket_id,
        pocket_name=pocket_name,
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


def _trigger_assignee_name(action: Action) -> str | None:
    """Extract a human-readable assignee name from the trigger source."""
    if action.trigger and action.trigger.type == "user":
        return action.trigger.source
    if action.assignee:
        return action.assignee
    return None


# Aliases the ``status`` query param accepts on top of the raw
# ``WorkItemStatus`` values. The frontend calls ``/items?status=pending``
# for the awaiting-approval feed; map it to the canonical status so the
# filter matches the projected items (which carry
# ``WorkItemStatus.AWAITING_APPROVAL``, not "pending").
_STATUS_FILTER_ALIASES: dict[str, str] = {
    "pending": WorkItemStatus.AWAITING_APPROVAL.value,
}


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


def _task_to_work_item(task: Any, workspace_id: str, pocket_name: str = "") -> WorkItem:
    """Project a ``Task`` (or its DTO) into a Mission Control ``WorkItem``.

    Accepts either a ``tasks.domain.Task`` or a ``TaskResponse`` DTO —
    both expose the same field names so attribute access works on either.

    ``blocked_by`` ids are prefixed with ``task:`` to match the
    WorkItem id convention — the frontend can resolve a dependency edge
    back to its WorkItem row without a translation step.
    """
    assignee = task.assignee
    assignee_kind = AssigneeKind.AGENT if assignee.kind == "agent" else AssigneeKind.USER
    assignee_name = assignee.name or assignee.id
    agent_name = assignee.name if assignee.kind == "agent" else ""
    status = _TASK_STATUS_MAP.get(task.status, WorkItemStatus.IN_PROGRESS)
    section = _task_section(task.status, assignee.kind)
    blocked_by_raw = getattr(task, "blocked_by", None) or ()
    blocked_by = tuple(f"task:{dep_id}" for dep_id in blocked_by_raw)
    due_at = getattr(task, "due_at", None)
    # The task may be a domain Task (due_at is datetime | None) or a
    # TaskResponse DTO (due_at is str | None). Normalise to datetime.
    if isinstance(due_at, str):
        try:
            due_at = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            due_at = None
    return WorkItem(
        id=f"task:{task.id}",
        workspace_id=workspace_id,
        section=section,
        status=status,
        title=task.title,
        description=task.summary or "",
        assignee_kind=assignee_kind,
        assignee_id=assignee.id,
        assignee_name=assignee_name,
        agent_id=assignee.id if assignee.kind == "agent" else None,
        agent_name=agent_name,
        pocket_id=task.pocket_id or None,
        pocket_name=pocket_name,
        source_kind="task",
        source_id=task.id,
        priority=task.priority,
        due_at=due_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        fabric_refs=(),
        blocked_by=blocked_by,
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
    name_map = await _pocket_name_map(ctx, project_id=body.project_id)

    items: list[WorkItem] = []

    # --- Instinct Nudges (pocket- or workspace-scoped) ----------------------
    # Most Nudges live inside a pocket and only surface when that pocket is
    # visible. External-action proposals are the exception: they stamp
    # ``pocket_id = workspace_id`` (see ``external_actions/propose.py``), so
    # they're admitted by workspace identity instead — which also means the
    # block must run even when the workspace has zero visible pockets. Tasks
    # below have their own workspace-level tenancy and are NOT gated by
    # pocket visibility.
    # ISO: HTTP path (no ``current_workspace`` ContextVar) — scope the store
    # file to the caller's workspace (the W4c in-row filter below is additive).
    store = get_instinct_store(workspace_id=workspace_id or None)
    # W4c — scope the instinct reads to the caller's workspace (plus legacy
    # NULL rows) at the store/SQL layer so a tenant never reads another
    # tenant's Nudges off the shared DB. The pocket-visibility filter below
    # is a second, intra-tenant layer (owner / shared_with / visibility);
    # the store scope is also what keeps OTHER tenants' workspace-scoped
    # nudges out — ``a.pocket_id == workspace_id`` below can only ever match
    # the caller's own rows.
    pending = await store.pending(pocket_id=body.pocket, workspace_id=workspace_id)
    resolved = await store.list_actions(pocket_id=body.pocket, limit=200, workspace_id=workspace_id)

    actions: list[Action] = []
    seen: set[str] = set()
    for a in (*pending, *resolved):
        if a.id in seen:
            continue
        if a.pocket_id not in visible and a.pocket_id != workspace_id:
            continue
        if body.agent and a.trigger.source != body.agent:
            continue
        seen.add(a.id)
        actions.append(a)
    # Batch-resolve every trigger.source (the proposer user id on a gated
    # Nudge) to a display name ONCE — a single _UserDoc.find over the union
    # of source ids, mirroring how name_map resolves pockets once — so The
    # Tray renders a person, not a raw ObjectId hex.
    actor_name_map = await _resolve_actor_names(
        {a.trigger.source for a in actions if a.trigger and a.trigger.source}
    )
    items.extend(
        _action_to_work_item(
            a,
            workspace_id,
            # A workspace-scoped nudge has no pocket — show "Workspace"
            # rather than leaking the raw workspace hex id as a name.
            pocket_name=(
                "Workspace"
                if a.pocket_id == workspace_id
                else name_map.get(a.pocket_id, a.pocket_id or "")
            ),
            actor_name_map=actor_name_map,
        )
        for a in actions
    )

    # --- Tasks (workspace-scoped) ------------------------------------------
    # Lazy import keeps the façade installable on forks that haven't
    # adopted the Tasks entity yet (matches the projects/_unassign_project
    # pattern). Tasks live alongside Nudges in the unified feed.
    try:
        from pocketpaw_ee.cloud.tasks import service as tasks_service
        from pocketpaw_ee.cloud.tasks.dto import ListTasksRequest
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
            items.append(
                _task_to_work_item(
                    t,
                    workspace_id,
                    pocket_name=name_map.get(t.pocket_id or "", t.pocket_id or ""),
                )
            )

    if body.section is not None:
        items = [it for it in items if it.section == body.section]
    # Honor the endpoint's documented ``status`` filter. Composes with the
    # section/agent/pocket filters above. ``"pending"`` is the frontend's
    # alias for the awaiting-approval state (the projection maps
    # ``ActionStatus.PENDING`` → ``WorkItemStatus.AWAITING_APPROVAL`` via
    # ``_status_to_section_status``), so it excludes terminal (done/failed)
    # items. ``status=None`` returns everything, unchanged.
    if body.status is not None:
        target = _STATUS_FILTER_ALIASES.get(body.status, body.status)
        items = [it for it in items if it.status.value == target]
    # Stable order: newest first by created_at, falling back to id.
    items.sort(key=lambda it: (it.created_at or datetime.min, it.id), reverse=True)
    return [work_item_to_response(it) for it in items[: body.limit]]


async def _execute_gated_bulk_approved_nudge(action: Any, *, ctx: RequestContext) -> bool:
    """Fire the apply-on-approve executor for a non-pocket-write gated Nudge.

    The 8 non-pocket-write gated proposal kinds (``_code_change`` /
    ``_external_action`` / ``_fabric_objects`` / ``_pocket_create`` /
    ``_instinct_rule`` / ``_belt_plan`` / ``_artifact_change`` /
    ``_admin_action``) each park a write that only lands when its OWN executor
    fires. The single-/bulk-approve HTTP path
    (``ee.instinct.router.bulk_approve_actions``) dispatches these per blob
    kind; the Mission Control façade must do the same or a bulk-approved
    admin / external / etc. Nudge flips to ``approved`` and STRANDS the write
    (no billing checkout url, no connector call, no admin write) — the exact
    execution gap the pocket-write path already closes.

    We reuse the router's blob accessors + the shared
    ``_code_change_proposed_event_id`` causation helper (lazy import — no
    module-top instinct→mission_control coupling) and each kind's own
    executor, which OWNS its Decision-Graph chain close. This mirrors the
    router's dispatch, it does not fork the executor logic (the executors are
    unchanged). Emits the per-item ``human.corrected(accepted)`` first
    (bulk-approve has no edit surface, so disposition is always ``accepted``),
    threading the ``agent.proposed`` id as causation, exactly like the router.

    Returns True when a gated (non-pocket-write) executor was dispatched,
    False when the Action carries no such blob (so the caller can fall through
    to the pocket-write path). Raising is left to the caller's per-item
    isolation wrapper.
    """
    from pocketpaw_ee.instinct.chain_emitters import _code_change_proposed_event_id
    from pocketpaw_ee.instinct.router import (
        _admin_action_blob,
        _artifact_change_blob,
        _belt_plan_blob,
        _code_change_blob,
        _emit_human_corrected,
        _external_action_blob,
        _fabric_objects_blob,
        _instinct_rule_blob,
        _pocket_create_blob,
    )

    workspace_id = ctx.workspace_id or ""

    # (blob-accessor, "module path", "executor attr") — same order + executors
    # the router's bulk_approve_actions dispatch uses. Lazy-imported per hit so
    # the façade keeps no module-top dependency on the executor packages.
    dispatch: list[tuple[Any, str, str]] = [
        (_code_change_blob, "pocketpaw_ee.cloud.belt.executor", "execute_approved_change"),
        (
            _external_action_blob,
            "pocketpaw_ee.cloud.external_actions.executor",
            "execute_approved_external_action",
        ),
        (
            _fabric_objects_blob,
            "pocketpaw_ee.cloud.fabric_proposals.executor",
            "execute_approved_fabric_objects",
        ),
        (
            _pocket_create_blob,
            "pocketpaw_ee.cloud.pocket_proposals.executor",
            "execute_approved_pocket_create",
        ),
        (
            _instinct_rule_blob,
            "pocketpaw_ee.cloud.instinct_rule_proposals.executor",
            "execute_approved_instinct_rule",
        ),
        (_belt_plan_blob, "pocketpaw_ee.cloud.mandates.executor", "execute_approved_plan"),
        (
            _artifact_change_blob,
            "pocketpaw_ee.versions.instinct_executor",
            "execute_approved_change",
        ),
        (
            _admin_action_blob,
            "pocketpaw_ee.cloud.admin_proposals.executor",
            "execute_approved_admin_action",
        ),
    ]

    for blob_of, module_path, executor_attr in dispatch:
        blob = blob_of(action)
        if blob is None:
            continue
        import importlib

        human_event_id = _emit_human_corrected(
            blob=blob,
            action=action,
            user_id=ctx.user_id,
            workspace_id=workspace_id,
            disposition="accepted",
            note=None,
            causation_override=_code_change_proposed_event_id(blob),
        )
        executor = getattr(importlib.import_module(module_path), executor_attr)
        await executor(action, human_event_id=human_event_id)
        return True

    return False


async def _execute_bulk_approved_nudge(action: Any, *, ctx: RequestContext) -> dict[str, Any]:
    """Execute one bulk-approved Nudge's parked write + emit chain.

    Mirrors the single-/bulk-approve HTTP path
    (``ee.instinct.router.bulk_approve_actions``) for ONE approved Action.
    Two families of gated proposal park a write that only lands when its
    executor fires:

      * the pocket-write bridge (``_pocket_write``) — emit
        ``human.corrected(accepted)`` + ``policy.evaluated(passed=True)`` then
        fire ``instinct_bridge.execute_approved_write`` (the bridge owns the
        chain close);
      * every OTHER gated kind (``_admin_action`` / ``_external_action`` /
        ``_code_change`` / ``_fabric_objects`` / ``_pocket_create`` /
        ``_instinct_rule`` / ``_belt_plan`` / ``_artifact_change``) —
        dispatched by ``_execute_gated_bulk_approved_nudge`` to that kind's own
        executor, which owns its chain close.

    Handling ALL gated kinds (not just pocket-write) is what makes a
    bulk-approved admin / external / etc. Nudge actually EXECUTE (e.g. a
    ``billing.manage`` admin action produces its Dodo checkout url) instead of
    flipping to ``approved`` and stranding the write forever.

    We reuse the shared chain-emit helpers + the router's blob accessors (lazy
    import — no module-top instinct→mission_control coupling) so the chain
    logic is shared, not forked, and the façade no longer reaches into the
    router's internals.

    Returns a per-item outcome dict ``{"id", "executed", "error"}``:
      - Actions with no parked write of any gated kind report
        ``executed=False`` with no error (flipping to ``approved`` is the
        whole action);
      - a gated Action reports ``executed=True`` on a clean dispatch, or
        ``executed=False`` + ``error`` when the execution raised.

    Error isolation: every executor is best-effort by contract (it records
    failures on the Action and never raises), but we still wrap the whole body
    so one item's unexpected crash can't strand the rest of the batch.
    """
    from pocketpaw_ee.cloud.pockets import instinct_bridge
    from pocketpaw_ee.instinct.chain_emitters import (
        _emit_human_corrected,
        _emit_policy_evaluated_approved,
        _pocket_write_blob,
    )

    action_id = str(getattr(action, "id", "") or "")
    workspace_id = ctx.workspace_id or ""

    # Every non-pocket-write gated kind dispatches to its own executor first,
    # exactly like the router's bulk-approve dispatch. On a hit the executor
    # ran (and owns its chain close) — report executed and stop.
    try:
        if await _execute_gated_bulk_approved_nudge(action, ctx=ctx):
            return {"id": action_id, "executed": True, "error": None}
    except Exception as exc:  # noqa: BLE001 — per-item isolation
        logger.exception(
            "mission_control.bulk_approve: gated execution failed for %s",
            action_id,
        )
        return {"id": action_id, "executed": False, "error": str(exc)}

    blob = _pocket_write_blob(action)
    if blob is None:
        # No parked write of any gated kind — the approval flip is the entire
        # effect. Nothing to execute, nothing to chain-emit.
        return {"id": action_id, "executed": False, "error": None}

    try:
        # Chain symmetry with the HTTP approve path: human.corrected first,
        # then a passing policy.evaluated whose causation points at it.
        human_event_id = _emit_human_corrected(
            blob=blob,
            action=action,
            user_id=ctx.user_id,
            workspace_id=workspace_id,
            disposition="accepted",
            note=None,
        )
        _emit_policy_evaluated_approved(
            blob=blob,
            action=action,
            user_id=ctx.user_id,
            workspace_id=workspace_id,
            causation_event_id=human_event_id,
        )
        # The bridge owns the chain close (``_emit_bridge_chain_close``)
        # after the post-approval write lands. It never raises by contract,
        # but the wrapper below keeps one bad item from dropping the rest.
        await instinct_bridge.execute_approved_write(action)
    except Exception as exc:  # noqa: BLE001 — per-item isolation
        logger.exception(
            "mission_control.bulk_approve: pocket-write execution failed for %s",
            action_id,
        )
        return {"id": action_id, "executed": False, "error": str(exc)}

    return {"id": action_id, "executed": True, "error": None}


async def agent_bulk_approve(
    ctx: RequestContext, body: BulkActionRequest | dict[str, Any]
) -> dict[str, Any]:
    """Approve N pending items in one call.

    Works for both Nudges (Instinct store) and Tasks (Tasks service).
    Routing is by id prefix, matching the heterogeneous WorkItem feed:
      - ``task:<id>``   → Tasks service's ``agent_complete_task``;
      - ``nudge:<id>``  → Instinct store's ``bulk_approve`` (prefix stripped);
      - bare ``<id>``   → Instinct store as a Nudge (mirrors
        ``agent_bulk_reject``, which passes bare ids straight through).

    The shared ``bulk_id`` lives in every audit row's ``context.bulk_id``
    so the operator can recover the bulk transaction.

    W0c fix — TWO defects:

    1. Routing regression — bare action ids were misrouted to the Tasks
       branch via ``_classify_task_id`` (whose forward-compat default
       treats a bare id as a Task). Bulk-approving a Nudge by its bare id
       (the shape the frontend + every existing test send) therefore
       never reached ``store.bulk_approve`` and silently landed in
       ``missing`` — the Nudge was never even approved. Routing is now by
       explicit prefix so bare ids approve as Nudges again, consistent
       with ``agent_bulk_reject``.

    2. Stranded writes — even once a Nudge approved, its parked pocket
       write never fired: the façade recorded the approval and stopped,
       never calling the Instinct bridge, so bulk-approved writes stalled
       at ``approved`` forever (no execution, no audit/chain emit) — the
       exact gap the single-approve path closes via
       ``execute_approved_write``. We now mirror that per item: each
       approved Nudge runs through ``_execute_bulk_approved_nudge`` (same
       chain emits + bridge call as ``ee.instinct.router.bulk_approve_actions``),
       with per-item error isolation so one failing write can't drop the
       rest. The per-item outcomes are reported under ``executed`` for the
       operator console.
    """
    from uuid import uuid4

    body = BulkActionRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    bulk_id = uuid4().hex
    approved: list[dict] = []
    missing: list[str] = []
    executed: list[dict] = []

    # Split IDs by prefix. ``task:`` → Tasks; ``nudge:`` or bare → Instinct
    # Nudge. ``nudge_id_map`` recovers the original wire id (with prefix)
    # for the ``missing`` report so the operator sees what they sent, while
    # the store receives the bare action id it stores rows under.
    task_ids: list[str] = []
    nudge_store_ids: list[str] = []
    nudge_id_map: dict[str, str] = {}
    for raw_id in body.ids:
        if raw_id.startswith("task:"):
            task_ids.append(raw_id)
            continue
        bare = raw_id[len("nudge:") :] if raw_id.startswith("nudge:") else raw_id
        nudge_store_ids.append(bare)
        nudge_id_map[bare] = raw_id

    # Handle tasks: call agent_complete_task for each
    if task_ids:
        from pocketpaw_ee.cloud.tasks import service as tasks_service
        from pocketpaw_ee.cloud.tasks.dto import CompleteTaskRequest

        for raw_id in task_ids:
            task_id = _classify_task_id(raw_id)
            if task_id is None:
                missing.append(raw_id)
                continue
            try:
                result = await tasks_service.agent_complete_task(
                    ctx,
                    task_id,
                    CompleteTaskRequest(next_action="archive"),
                )
                approved.append(result.model_dump(mode="json"))
            except Exception as e:
                logger.info("bulk_approve: task %s failed: %s", raw_id, e)
                missing.append(raw_id)

    # Handle nudges via Instinct store
    if nudge_store_ids:
        visible = await _visible_pocket_ids(ctx)
        # ISO: HTTP path (no ContextVar) — scope the store to the caller.
        store = get_instinct_store(workspace_id=workspace_id or None)
        eligible, blocked = await _split_ids_by_tenancy(
            store, nudge_store_ids, visible, workspace_id
        )
        nudge_approved, nudge_missing, _ = await store.bulk_approve(
            eligible, approver=ctx.user_id, note=body.note
        )
        approved.extend(a.model_dump(mode="json") for a in nudge_approved)
        # Report missing / blocked under the operator's original wire ids.
        missing.extend(nudge_id_map.get(mid, mid) for mid in nudge_missing)
        missing.extend(nudge_id_map.get(bid, bid) for bid in blocked)

        # W0c — execute each approved Nudge's parked write + emit its chain,
        # preserving the bulk_approve ordering and isolating per-item errors.
        for action in nudge_approved:
            executed.append(await _execute_bulk_approved_nudge(action, ctx=ctx))

    _record_deep_work_audit(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.item.approved",
        target_type="bulk",
        metadata={
            "bulk_id": bulk_id,
            "approved": approved,
            "missing": missing,
            "executed": executed,
        },
    )
    return {
        "bulk_id": bulk_id,
        "approved": approved,
        "missing": missing,
        "executed": executed,
    }


async def agent_bulk_reject(
    ctx: RequestContext, body: BulkActionRequest | dict[str, Any]
) -> dict[str, Any]:
    """Reject N pending items in one call. ``reason`` is required.

    Handles both Tasks (``task:`` prefix) and Instinct Nudges (bare or
    ``nudge:`` prefix) — mirrors the same split that ``agent_bulk_approve``
    performs. Tasks are blocked with the given reason so they surface in
    the operator's Snags section; Nudges flow through the Instinct store's
    native reject path.

    The reason text is surfaced on every Action's ``rejected_reason`` AND
    on every audit row's ``context.reason`` so the soul-bridge correction
    pipeline can learn from bulk rejects the same way it learns from
    single-item rejects.
    """
    body = BulkActionRequest.model_validate(body)
    if not body.reason:
        raise ValidationError(
            "mission_control.reason_required",
            "bulk-reject requires a reason — pass a non-empty string in ``reason``.",
        )
    workspace_id = _require_workspace(ctx)

    from uuid import uuid4

    bulk_id = uuid4().hex
    rejected: list[dict] = []
    missing: list[str] = []

    # Split IDs by prefix — same pattern as agent_bulk_approve
    task_ids: list[str] = []
    nudge_store_ids: list[str] = []
    nudge_id_map: dict[str, str] = {}
    for raw_id in body.ids:
        if raw_id.startswith("task:"):
            task_ids.append(raw_id)
            continue
        bare = raw_id[len("nudge:") :] if raw_id.startswith("nudge:") else raw_id
        nudge_store_ids.append(bare)
        nudge_id_map[bare] = raw_id

    # Handle tasks: block each one so it surfaces in Snags
    if task_ids:
        from pocketpaw_ee.cloud.tasks import service as tasks_service
        from pocketpaw_ee.cloud.tasks.dto import BlockTaskRequest

        for raw_id in task_ids:
            task_id = _classify_task_id(raw_id)
            if task_id is None:
                missing.append(raw_id)
                continue
            try:
                result = await tasks_service.agent_block_task(
                    ctx,
                    task_id,
                    BlockTaskRequest(reason=body.reason),
                )
                rejected.append(result.model_dump(mode="json"))
            except Exception as e:
                logger.info("bulk_reject: task %s failed: %s", raw_id, e)
                missing.append(raw_id)

    # Handle nudges via Instinct store
    if nudge_store_ids:
        visible = await _visible_pocket_ids(ctx)
        # ISO: HTTP path (no ContextVar) — scope the store to the caller.
        store = get_instinct_store(workspace_id=workspace_id or None)
        eligible, blocked = await _split_ids_by_tenancy(
            store, nudge_store_ids, visible, workspace_id
        )
        nudge_rejected, nudge_missing, _ = await store.bulk_reject(
            eligible, reason=body.reason, rejector=ctx.user_id
        )
        rejected.extend(a.model_dump(mode="json") for a in nudge_rejected)
        missing.extend(nudge_id_map.get(mid, mid) for mid in nudge_missing)
        missing.extend(nudge_id_map.get(bid, bid) for bid in blocked)

    # no-event: per-item block/reject inside the loops already emit events
    _record_deep_work_audit(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.item.rejected",
        target_type="bulk",
        metadata={"bulk_id": bulk_id, "rejected": rejected, "missing": missing},
    )
    return {
        "bulk_id": bulk_id,
        "rejected": rejected,
        "missing": missing,
    }


async def _split_ids_by_tenancy(
    store: Any, ids: list[str], visible_pockets: set[str], workspace_id: str
) -> tuple[list[str], list[str]]:
    """Partition ``ids`` into (visible-to-caller, blocked).

    Reads each Action once to look up its pocket. Cheap for the bulk
    sizes Mission Control surfaces (UI selection is bounded by the page
    of items the operator sees). Missing rows fall on the eligible side
    so Instinct's store returns them in its own ``missing`` slot and the
    bulk-action response carries a single deduplicated list.

    Tenancy must match ``agent_list_work_items`` EXACTLY — the list path
    admits an action if ``a.pocket_id in visible OR a.pocket_id ==
    workspace_id``. The second clause is what surfaces WORKSPACE-SCOPED
    nudges (``pocket_id == workspace_id`` — e.g. ``_admin_action`` /
    ``_external_action`` proposals, which aren't pocket-bound). Without the
    same clause here, a workspace-scoped nudge that LISTS in The Tray would
    be pushed to ``blocked`` on approve and reported ``missing`` — the
    proposal would stay pending forever. Admitting ``pocket_id ==
    workspace_id`` can only ever match the CALLER'S own workspace: the
    store read is already W4c/ISO-2 tenant-scoped, so another tenant's
    workspace-scoped nudge never reaches this loop as an approvable row.
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
        if action.pocket_id in visible_pockets or action.pocket_id == workspace_id:
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
    workspace_id = _require_workspace(ctx)
    visible = await _visible_pocket_ids(ctx)
    # ISO: HTTP path (no ContextVar) — scope the store to the caller.
    store = get_instinct_store(workspace_id=workspace_id or None)
    cutoff = datetime.now() - _window_to_delta(body.window)

    # Pull a generous slice and filter in Python. ``list_actions`` does
    # ORDER BY created_at DESC LIMIT, so the slice is the newest N.
    # W4c — this read has NO pocket filter at the store, so before W4c the
    # newest 500 rows on a shared DB could be entirely OTHER tenants' — both a
    # cross-tenant leak AND a correctness bug (the caller's rows starved out of
    # the window). Scope to ``workspace_id`` so the slice is this tenant's
    # newest 500 (plus legacy NULL rows); the ``pocket_id in visible`` filter
    # below stays as the intra-tenant layer.
    actions = await store.list_actions(limit=500, workspace_id=workspace_id)
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
        agent_name=e.agent_name,
        summary=e.summary,
        pocket_id=e.pocket_id,
        pocket_name=e.pocket_name,
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

    from pocketpaw_ee.cloud.tasks import service as tasks_service
    from pocketpaw_ee.cloud.tasks.dto import ReassignTaskRequest

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
    _record_deep_work_audit(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.item.reassigned",
        target_type="bulk",
        metadata={"bulk_id": bulk_id, "affected": affected, "skipped": skipped},
    )
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

    from pocketpaw_ee.cloud.tasks import service as tasks_service
    from pocketpaw_ee.cloud.tasks.dto import UpdateTaskRequest

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
    _record_deep_work_audit(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.item.snoozed",
        target_type="bulk",
        metadata={
            "bulk_id": bulk_id,
            "affected": affected,
            "skipped": skipped,
            "until_iso": body.until_iso,
        },
    )
    return {"bulk_id": bulk_id, "affected": affected, "skipped": skipped}


async def agent_bulk_revert(
    ctx: RequestContext, body: BulkRevertRequest | dict[str, Any]
) -> dict[str, Any]:
    """Revert N Tasks from a terminal status back to in_progress.

    Fans out per-id to ``tasks.service.agent_revert_task`` to flip the
    task status from ``done``, ``reverted``, or ``failed`` back to
    ``in_progress``. Ids that aren't Tasks land in ``skipped``.

    This differs from ``agent_bulk_reject`` (which rejects pending
    Nudges) — revert acts on *already-finished* work to un-mark it
    as complete so the operator can resume work.
    """
    body = BulkRevertRequest.model_validate(body)
    _require_workspace(ctx)

    from uuid import uuid4

    from pocketpaw_ee.cloud.tasks import service as tasks_service

    bulk_id = uuid4().hex
    affected: list[str] = []
    skipped: list[str] = []

    for raw_id in body.ids:
        task_id = _classify_task_id(raw_id)
        if task_id is None:
            skipped.append(raw_id)
            continue
        try:
            await tasks_service.agent_revert_task(ctx, task_id)
            affected.append(raw_id)
        except Exception:
            logger.info(
                "mission_control.bulk_revert: skipped id %s",
                raw_id,
                exc_info=True,
            )
            skipped.append(raw_id)

    _record_deep_work_audit(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.item.reverted",
        target_type="bulk",
        metadata={"bulk_id": bulk_id, "affected": affected, "skipped": skipped},
    )
    return {"bulk_id": bulk_id, "affected": affected, "skipped": skipped}


# ---------------------------------------------------------------------------
# Plan sessions — drafts list for the Mission Control Plan tab
# ---------------------------------------------------------------------------


# Doc-level → wire status. ``ready`` plans are the current materialized
# plan for a project (operator can still review + ship), so they surface
# as ``draft`` in the drafts list. ``stale`` plans were superseded by a
# re-plan or marked outdated, so they land in ``archived``. There is no
# ``active`` state in the doc today — reserved for "plan currently
# executing" once the runtime ships it.
_WIRE_STATUS_BY_DOC: dict[str, str] = {
    "ready": "draft",
    "stale": "archived",
}

# Inverse map for filtering: the wire filter ``?status=draft`` reads
# back as the doc-level ``ready`` query. Unknown wire values produce
# ``None`` and the service returns an empty list — the DTO regex on
# the wire enum already catches typos before we get here.
_DOC_STATUS_BY_WIRE: dict[str, str] = {v: k for k, v in _WIRE_STATUS_BY_DOC.items()}


def _plan_session_to_dto(summary: Any) -> PlanSessionDTO:
    """Map a ``PlanSessionSummary`` domain object to its wire DTO.

    Status falls back to ``draft`` when the doc carries an unknown
    value — defensive against future doc-level statuses we haven't
    taught Mission Control about yet. Timestamps are serialized to
    ISO-8601 with timezone so the frontend doesn't have to coerce
    naive datetimes.
    """
    wire_status = _WIRE_STATUS_BY_DOC.get(summary.status, "draft")
    return PlanSessionDTO(
        id=summary.id,
        project_id=summary.project_id,
        name=summary.name,
        status=wire_status,  # type: ignore[arg-type]
        task_count=summary.task_count,
        created_at=summary.created_at.isoformat(),
        updated_at=summary.updated_at.isoformat(),
    )


async def agent_list_plan_sessions(
    ctx: RequestContext, body: ListPlanSessionsRequest | dict[str, Any]
) -> PlanSessionListResponse:
    """List the workspace's persisted plan sessions for the drafts list.

    Read-only — no Beanie writes here. Delegates to
    ``planner.service.list_plan_sessions`` (the entity that owns the
    PlanSession doc per ee/cloud Rule 2) and wire-maps the typed
    summaries into the response envelope.

    Tenancy:
      - ``ctx.workspace_id`` is the source of truth; an empty / missing
        workspace returns an empty envelope rather than 500ing. Routers
        reject ``?workspace_id=`` query params before we get here.

    # no-event: read-only per Rule 9.
    """
    body = ListPlanSessionsRequest.model_validate(body)
    if not ctx.workspace_id:
        return PlanSessionListResponse(sessions=[], total=0)

    # Lazy import so the façade still installs cleanly on forks that
    # disabled the planner entity (mirrors the Tasks branch in
    # ``agent_list_work_items``). When planner is missing we surface an
    # empty list — the drafts tab renders the empty-state copy without
    # crashing the whole MC console.
    try:
        from pocketpaw_ee.cloud.planner import service as planner_service
    except ImportError:
        logger.info("mission_control.plan_sessions: planner entity not installed")
        return PlanSessionListResponse(sessions=[], total=0)

    doc_status: str | None = None
    if body.status is not None:
        doc_status = _DOC_STATUS_BY_WIRE.get(body.status)
        if doc_status is None:
            # Wire status that doesn't map to any doc state today
            # (``active`` until the runtime ships it). Return empty so
            # the frontend doesn't break when the operator filters by
            # a reserved-but-empty bucket.
            return PlanSessionListResponse(sessions=[], total=0)

    summaries = await planner_service.list_plan_sessions(ctx, status=doc_status, limit=body.limit)
    dtos = [_plan_session_to_dto(s) for s in summaries]
    return PlanSessionListResponse(sessions=dtos, total=len(dtos))


# ---------------------------------------------------------------------------
# Cycles — workspace-scoped create for the Mission Control rail's
# "+ New cycle" button.
# ---------------------------------------------------------------------------


def _parse_wire_date(value: str, *, field_name: str) -> date:
    """Parse an ISO-8601 date or datetime string into a ``date``.

    The wire takes either "2026-05-19" (the raw <input type="date"> value
    the frontend posts) or "2026-05-19T12:00:00Z" (a datetime, in case a
    different caller serializes a JS ``Date`` straight to ISO). Invalid
    strings surface as a 422 ``cycle.invalid_date`` so the operator gets
    a clear error rather than a 500.

    We accept both the bare date and the datetime forms by trying date
    first then datetime — fromisoformat handles each in one pass without
    a regex or third-party parser.
    """
    try:
        # date.fromisoformat handles "2026-05-19" cleanly. It rejects
        # "2026-05-19T12:00:00Z", which falls through to the datetime
        # path below.
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        # Coerce trailing "Z" to "+00:00" so datetime.fromisoformat
        # accepts the common JS toISOString() output.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValidationError(
            "cycle.invalid_date",
            f"{field_name} must be an ISO-8601 date or datetime; got {value!r}",
        ) from exc


def _derive_status_from_dates(start: date, end: date, *, today: date | None = None) -> str:
    """Derive a cycle's status from its date range relative to today.

    Rule (per spec): a cycle whose ``start`` is in the future is
    ``upcoming``; one whose ``start`` has passed and ``end`` hasn't is
    ``active``. ``completed`` is intentionally NOT derived here — it's
    set by the separate close workflow (``cycles.service.agent_close_cycle``)
    or by the daily snapshot job's auto-rollover, never by create.

    A cycle whose ``end`` is already in the past at create time falls
    through to ``upcoming`` — backfilling historical cycles isn't a
    create-time concern, so we don't silently promote them to a
    terminal state.
    """
    today = today or datetime.now(UTC).date()
    if start <= today < end:
        return "active"
    return "upcoming"


async def agent_create_cycle(ctx: RequestContext, body: CreateCycleRequest | dict[str, Any]) -> Any:
    """Create a cycle in the caller's workspace from the Mission Control rail.

    Mirrors the audit + plan-sessions pattern: workspace tenancy from
    ``ctx``, wire-friendly string dates, status derived from the parsed
    range. The actual Beanie write happens inside
    ``cycles.service.agent_create_cycle`` — single owner per ee/cloud
    Rule 2, enforced by an import-linter contract that forbids
    ``ee.cloud.models.cycle`` from this façade module.

    Behavior:
      - Reads ``ctx.workspace_id`` (rejected ``?workspace_id`` upstream
        in the router).
      - Parses ``start`` / ``end`` from ISO strings; surfaces invalid
        strings as 422 ``cycle.invalid_date``.
      - Requires ``start < end`` — 422 ``cycle.invalid_date_range`` when
        violated.
      - Derives ``status`` from the dates: future → ``upcoming``;
        spanning now → ``active``.
      - Delegates project tenancy + the actual write to the cycles
        service, which already enforces project-in-workspace and emits
        ``cycle.created`` on the bus.

    Returns the cycles entity's ``CycleResponse`` directly — the
    frontend's existing listCycles row shape matches verbatim so
    ``cycles.unshift(response)`` works without re-fetch.
    """
    body = CreateCycleRequest.model_validate(body)

    start = _parse_wire_date(body.start, field_name="start")
    end = _parse_wire_date(body.end, field_name="end")
    if start >= end:
        raise ValidationError(
            "cycle.invalid_date_range",
            "start must be before end",
        )

    status = _derive_status_from_dates(start, end)

    # Lazy import keeps the façade installable on forks that disabled
    # the cycles entity (same pattern as the planner / tasks branches
    # above). When cycles is missing we raise a clear 422 rather than
    # a 500 — the operator's frontend renders the message.
    try:
        from pocketpaw_ee.cloud.cycles import service as cycles_service
        from pocketpaw_ee.cloud.cycles.dto import CreateCycleRequest as CyclesCreateRequest
    except ImportError as exc:
        raise ValidationError(
            "cycle.entity_unavailable",
            "Cycles entity is not installed on this deployment.",
        ) from exc

    cycles_body = CyclesCreateRequest(
        name=body.name,
        description="",
        pocket_id=None,
        project_id=body.project_id,
        start=start,
        end=end,
        status=status,  # type: ignore[arg-type]
        scope=body.scope,
    )
    # The cycles service performs the Beanie write, validates project
    # tenancy via ``_ensure_project_in_workspace`` (raises NotFound when
    # the project isn't in this workspace), and emits ``cycle.created``
    # — no second emit needed from here per Rule 9.
    result = await cycles_service.agent_create_cycle(ctx, cycles_body)
    _record_deep_work_audit(
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.cycle.created",
        target_type="cycle",
        target_id=str(result.id) if hasattr(result, "id") else body.name,
        metadata={"name": body.name, "project_id": body.project_id, "scope": body.scope},
    )
    return result


async def agent_attach_cycle_items(
    ctx: RequestContext,
    cycle_id: str,
    body: AttachCycleItemsRequest,
) -> AttachCycleItemsResponse:
    """Attach a batch of existing work items to a sprint.

    Validates the sprint exists in the caller's workspace, then for each
    item id calls the permission-relaxed ``tasks.service.agent_set_task_cycle``
    helper. Items the caller can't see (wrong workspace, deleted, etc.)
    are reported back as ``skipped`` rather than failing the whole batch.
    """

    body = AttachCycleItemsRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    # Lazy imports keep the cross-entity coupling on the call path so the
    # ee/cloud entity-boundary lint can't get tripped on a top-level import.
    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.cloud.cycles import service as cycles_service
    from pocketpaw_ee.cloud.tasks import service as tasks_service

    # Tenancy check on the cycle itself: this raises NotFound if the sprint
    # isn't in the caller's workspace, so the response can't mislead.
    await cycles_service._fetch_in_workspace(workspace_id, cycle_id)

    attached: list[str] = []
    skipped: list[str] = []
    for task_id in body.item_ids:
        try:
            # The mission-control facade prefixes task IDs with ``task:``
            # in ``_task_to_work_item`` — strip it before passing to the
            # tasks service, which expects a raw MongoDB ObjectId hex string.
            raw_id = task_id.removeprefix("task:")
            await tasks_service.agent_set_task_cycle(ctx, raw_id, cycle_id)
            attached.append(task_id)
        except NotFound:
            skipped.append(task_id)

    _record_deep_work_audit(
        workspace_id=workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.cycle.items_attached",
        target_type="cycle",
        target_id=cycle_id,
        metadata={"attached": attached, "skipped": skipped},
    )
    return AttachCycleItemsResponse(
        attached=attached,
        skipped=skipped,
        cycle_id=cycle_id,
    )


async def agent_detach_cycle_items(
    ctx: RequestContext,
    cycle_id: str,
    body: AttachCycleItemsRequest,
) -> DetachCycleItemsResponse:
    """Detach a batch of work items from a sprint.

    Validates the sprint exists in the caller's workspace, then for each
    item id calls ``tasks.service.agent_set_task_cycle`` with ``None``
    to clear the cycle pointer. Items the caller can't see are reported
    back as ``skipped`` rather than failing the whole batch.
    """

    body = AttachCycleItemsRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    from pocketpaw_ee.cloud.cycles import service as cycles_service
    from pocketpaw_ee.cloud.tasks import service as tasks_service

    # Tenancy check on the cycle itself
    await cycles_service._fetch_in_workspace(workspace_id, cycle_id)

    detached: list[str] = []
    skipped: list[str] = []
    for task_id in body.item_ids:
        try:
            raw_id = task_id.removeprefix("task:")
            await tasks_service.agent_set_task_cycle(ctx, raw_id, None)
            detached.append(task_id)
        except Exception:
            skipped.append(task_id)

    # Refresh counters on the cycle so scope/started/completed update
    tasks = await cycles_service._tasks_for_cycle(ctx, cycle_id)
    if tasks is not None:
        scope, started, completed = cycles_service._counters_from_tasks(tasks)
        doc = await cycles_service._fetch_in_workspace(workspace_id, cycle_id)
        if (doc.scope, doc.started, doc.completed) != (scope, started, completed):
            doc.scope = scope
            doc.started = started
            doc.completed = completed
            await doc.save()

    _record_deep_work_audit(
        workspace_id=workspace_id,
        actor_id=ctx.user_id or "system",
        action="deep_work.cycle.items_detached",
        target_type="cycle",
        target_id=cycle_id,
        metadata={"detached": detached, "skipped": skipped},
    )
    return DetachCycleItemsResponse(
        detached=detached,
        skipped=skipped,
        cycle_id=cycle_id,
    )


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


async def agent_analytics(ctx: RequestContext, window: str = "7d") -> AnalyticsResponse:
    """Compute the operator analytics dashboard for the given window."""
    _require_workspace(ctx)
    from pocketpaw_ee.cloud.models.task import Task as _TaskDoc

    cutoff = datetime.now(tz=UTC) - _window_to_delta(window)

    # Fetch all tasks that were updated within the window
    docs = (
        await _TaskDoc.find(
            {
                "workspace_id": ctx.workspace_id,
                "updatedAt": {"$gte": cutoff},
            }
        )
        .sort(-_TaskDoc.updatedAt)
        .to_list()
    )

    shipped: list[_TaskDoc] = [d for d in docs if d.status == "done"]
    reverted: list[_TaskDoc] = [d for d in docs if d.status == "reverted"]

    total_shipped = len(shipped)
    total_reverted = len(reverted)
    total_decided = total_shipped + total_reverted
    approval_rate = round((total_shipped / total_decided * 100) if total_decided > 0 else 100, 1)
    revert_rate = round((total_reverted / total_decided * 100) if total_decided > 0 else 0, 1)

    # Latency: time from creation to completion for shipped tasks
    latencies = [
        (d.updatedAt - d.createdAt).total_seconds()
        for d in shipped
        if d.updatedAt and d.createdAt and d.updatedAt > d.createdAt
    ]
    latencies.sort()
    n = len(latencies)
    latency_p50 = latencies[max(0, min(n - 1, int(n * 0.5)))] if n > 0 else 0.0
    latency_p90 = latencies[max(0, min(n - 1, int(n * 0.9)))] if n > 0 else 0.0

    # Per-day breakdown
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_map: dict[str, int] = {}
    for d in shipped:
        if d.updatedAt:
            day_name = day_names[d.updatedAt.weekday()]
            day_map[day_name] = day_map.get(day_name, 0) + 1
    per_day = [
        AnalyticsDayDTO(day=name, shipped=day_map.get(name, 0))
        for name in ["Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue"]
    ]

    # By agent
    agent_map: dict[str, dict[str, int]] = {}
    for d in shipped:
        name = d.assignee.name or d.assignee.id
        agent_map.setdefault(name, {"shipped": 0, "reverted": 0})
        agent_map[name]["shipped"] += 1
    for d in reverted:
        name = d.assignee.name or d.assignee.id
        agent_map.setdefault(name, {"shipped": 0, "reverted": 0})
        agent_map[name]["reverted"] += 1
    by_agent = [
        AnalyticsAgentDTO(agent=name, shipped=v["shipped"], reverted=v["reverted"])
        for name, v in sorted(agent_map.items(), key=lambda x: -x[1]["shipped"])
    ]

    # By pocket — resolve pocket names via the pockets service
    pockets = await pockets_service.list_pockets(ctx.workspace_id, ctx.user_id)
    pocket_name_map: dict[str, str] = {
        p["_id"]: p.get("name", p["_id"]) for p in pockets if p.get("_id")
    }

    pocket_map: dict[str, int] = {}
    for d in shipped:
        pid = d.pocket_id or "__unknown__"
        pocket_map[pid] = pocket_map.get(pid, 0) + 1
    total_pocket = sum(pocket_map.values())
    by_pocket = [
        AnalyticsPocketDTO(
            pocket=pocket_name_map.get(pid, pid),
            shipped=count,
            share=round(count / total_pocket * 100, 1) if total_pocket > 0 else 0,
        )
        for pid, count in sorted(pocket_map.items(), key=lambda x: -x[1])
    ]

    return AnalyticsResponse(
        shipped=total_shipped,
        approval_rate=approval_rate,
        revert_rate=revert_rate,
        latency_p50_seconds=latency_p50,
        latency_p90_seconds=latency_p90,
        per_day=per_day,
        by_agent=by_agent,
        by_pocket=by_pocket,
    )


__all__ = [
    "agent_analytics",
    "agent_attach_cycle_items",
    "agent_bulk_approve",
    "agent_bulk_reassign",
    "agent_bulk_reject",
    "agent_bulk_revert",
    "agent_bulk_snooze",
    "agent_create_cycle",
    "agent_list_activity",
    "agent_list_plan_sessions",
    "agent_list_work_items",
    "agent_outcomes_summary",
]
