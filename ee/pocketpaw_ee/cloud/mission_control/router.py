# ee/pocketpaw_ee/cloud/mission_control/router.py — REST surface for the
# Mission Control façade: the heterogeneous work-item feed (Instinct Nudges +
# Tasks), bulk approve / reject / reassign / snooze / revert, outcomes,
# activity, plan sessions, and cycles. Every handler delegates to
# ``mission_control.service``.
#
# Invariants:
#   * Deciding a Nudge (bulk approve / reject) needs ``instinct.approve``
#     (ADMIN), the same gate as ``/instinct/actions/{id}/approve``. The check
#     runs only when the batch holds a Nudge id (bare or ``nudge:``), so a
#     MEMBER can still bulk-complete or block their own Tasks, which the Tasks
#     service gates by creator / assignee.
#   * Tenancy comes from the auth context. Query params that would name a
#     workspace (``?workspace_id``) are refused before DTO construction.
#   * Bad enum / timestamp input surfaces as a 422 ``ValidationError``, never
#     a raw pydantic error (HTTP 500).
"""Mission Control façade router.

Thin per ee/cloud rule #4 — parses requests, delegates to
``ee.cloud.mission_control.service``, returns DTOs. Errors flow as
``CloudError`` (rule #10) — never ``HTTPException`` from here.

Mount point: ``mount_cloud`` includes this router at ``/api/v1`` so the
canonical URLs are:

  GET  /api/v1/mission-control/items
  POST /api/v1/mission-control/items/bulk-approve
  POST /api/v1/mission-control/items/bulk-reject
  POST /api/v1/mission-control/items/bulk-reassign
  POST /api/v1/mission-control/items/bulk-snooze
  GET  /api/v1/mission-control/outcomes
  GET  /api/v1/mission-control/activity
  GET  /api/v1/mission-control/plan-sessions
  POST /api/v1/mission-control/cycles
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import current_user, require_action_any_workspace
from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.cloud.cycles.dto import CycleResponse
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.mission_control import service as mc_service
from pocketpaw_ee.cloud.mission_control.domain import WorkItemSection
from pocketpaw_ee.cloud.mission_control.dto import (
    ActivityEventResponse,
    AnalyticsResponse,
    AttachCycleItemsRequest,
    AttachCycleItemsResponse,
    BulkActionRequest,
    BulkReassignRequest,
    BulkRevertRequest,
    BulkSnoozeRequest,
    CreateCycleRequest,
    DetachCycleItemsRequest,
    DetachCycleItemsResponse,
    ListActivityRequest,
    ListPlanSessionsRequest,
    ListWorkItemsRequest,
    OutcomesQueryRequest,
    OutcomeSummaryResponse,
    PlanSessionListResponse,
    PlanSessionStatus,
    WorkItemResponse,
)

router = APIRouter(
    prefix="/mission-control",
    tags=["Mission Control"],
    dependencies=[Depends(require_license)],
)


@router.get("/items", response_model=list[WorkItemResponse])
async def list_items(
    section: str | None = Query(None),
    agent: str | None = Query(None),
    pocket: str | None = Query(None),
    project_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[WorkItemResponse]:
    """Workspace-aware work item feed.

    Filters compose: ``section`` narrows to one pane; ``agent``, ``pocket``,
    ``project_id``, and ``status`` further restrict; ``limit`` caps the
    projected list. Pass ``project_id`` as an empty string to filter for
    "no project assigned". ``status=pending`` returns only the
    awaiting-approval feed (excludes terminal done/failed items); omit
    ``status`` for the full feed.
    """
    # Validate ``section`` at the boundary so an out-of-enum value degrades to
    # a clean 422 instead of the raw pydantic ``ValidationError`` (HTTP 500)
    # that building ``ListWorkItemsRequest`` would otherwise raise. Mirrors the
    # lenient ``status`` handling (typed ``str``, normalised in the service).
    if section is not None and section not in WorkItemSection.__members__.values():
        valid = ", ".join(s.value for s in WorkItemSection)
        raise ValidationError(
            "mission_control.invalid_section",
            f"section must be one of: {valid}",
        )
    body = ListWorkItemsRequest(
        section=section,
        agent=agent,
        pocket=pocket,
        project_id=project_id,
        status=status,
        limit=limit,
    )
    return await mc_service.agent_list_work_items(ctx, body)


_instinct_approve_guard = require_action_any_workspace("instinct.approve")


async def _require_nudge_decision(user: Any, ctx: RequestContext, ids: list[str]) -> None:
    """Refuse a batch that decides a Nudge unless the caller holds
    ``instinct.approve`` in the request's workspace.

    The service routes every non-``task:`` id to the Instinct store, so any
    such id makes this an Instinct decision. Runs the same guard as the
    single-approve route (role, per-member override, audited denial) and
    denies the whole batch rather than silently dropping the Nudges.
    """
    if all(i.startswith("task:") for i in ids):
        return
    await _instinct_approve_guard(user=user, workspace_id=ctx.workspace_id or "")


@router.post("/items/bulk-approve")
async def bulk_approve(
    body: BulkActionRequest,
    ctx: RequestContext = Depends(request_context),
    user: Any = Depends(current_user),
) -> dict:
    """Approve N pending Nudges / complete N Tasks.

    Returns ``{bulk_id, approved, missing, executed}``. 403 when the batch
    holds a Nudge and the caller lacks ``instinct.approve``.
    """
    await _require_nudge_decision(user, ctx, body.ids)
    return await mc_service.agent_bulk_approve(ctx, body)


@router.post("/items/bulk-reject")
async def bulk_reject(
    body: BulkActionRequest,
    ctx: RequestContext = Depends(request_context),
    user: Any = Depends(current_user),
) -> dict:
    """Reject N pending Nudges / block N Tasks. Requires non-empty ``reason``.

    Returns ``{bulk_id, rejected, missing}``. The reason text lands on
    every Action's ``rejected_reason`` AND on every audit row's
    ``context.reason`` for soul-bridge replay. 403 when the batch holds a
    Nudge and the caller lacks ``instinct.approve``.
    """
    await _require_nudge_decision(user, ctx, body.ids)
    return await mc_service.agent_bulk_reject(ctx, body)


@router.post("/items/bulk-reassign")
async def bulk_reassign(
    body: BulkReassignRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Reassign N Tasks in one call.

    Delegates per-id to ``tasks.service.agent_reassign_task``. Returns
    ``{bulk_id, affected, skipped}``: ids that aren't Tasks (Nudges, any
    non-Task prefix) land in ``skipped``. Cross-workspace ids also land
    in ``skipped`` rather than raising, so a mixed operator selection
    can't leak across tenants.
    """
    return await mc_service.agent_bulk_reassign(ctx, body)


@router.post("/items/bulk-snooze")
async def bulk_snooze(
    body: BulkSnoozeRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Snooze N Tasks to a future ``until_iso`` timestamp.

    Delegates per-id to ``tasks.service.agent_update_task`` setting the
    Task's ``due_at`` to the snooze deadline. Same ``skipped`` semantics
    as ``bulk-reassign``. Invalid ISO timestamps return 422 via
    ``mission_control.invalid_until_iso``.
    """
    return await mc_service.agent_bulk_snooze(ctx, body)


@router.post("/items/bulk-revert")
async def bulk_revert(
    body: BulkRevertRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Revert N Tasks from a terminal status back to in_progress.

    Flips task status from ``done``, ``reverted``, or ``failed`` back to
    ``in_progress`` so the operator can resume work. Ids that aren't
    Tasks land in ``skipped``.
    """
    return await mc_service.agent_bulk_revert(ctx, body)


@router.get("/outcomes", response_model=OutcomeSummaryResponse)
async def outcomes(
    window: str = Query("24h", pattern=r"^(1h|24h|7d)$"),
    ctx: RequestContext = Depends(request_context),
) -> OutcomeSummaryResponse:
    """Aggregated outcome counts over ``window`` (1h | 24h | 7d)."""
    body = OutcomesQueryRequest(window=window)
    return await mc_service.agent_outcomes_summary(ctx, body)


@router.get("/activity", response_model=list[ActivityEventResponse])
async def activity(
    limit: int = Query(30, ge=1, le=200),
    ctx: RequestContext = Depends(request_context),
) -> list[ActivityEventResponse]:
    """In-memory live activity feed for the workspace.

    Bounded ring buffer (~200 entries) with a 1-hour TTL. Restart wipes
    history by design — durability lives in Pawprints.
    """
    body = ListActivityRequest(limit=limit)
    return await mc_service.agent_list_activity(ctx, body)


@router.post("/cycles", response_model=CycleResponse)
async def create_cycle(
    request: Request,
    body: CreateCycleRequest,
    ctx: RequestContext = Depends(request_context),
) -> CycleResponse:
    """Create a workspace-scoped cycle from the Mission Control rail.

    Mirrors the audit + plan-sessions endpoint patterns:
      - Tenancy lives on the auth ctx; passing ``?workspace_id`` is a
        400 ``cycles.workspace_id_forbidden`` rather than a silent leak.
      - The wire body uses ISO-8601 strings for ``start`` / ``end`` so
        a raw ``<input type="date">`` value posts directly.
      - ``status`` is derived in the service from the parsed dates
        (``upcoming`` vs ``active``); the wire intentionally doesn't
        accept a status — the rail's flow is "create a new one" not
        "backfill historical state".

    The handler delegates straight to ``mc_service.agent_create_cycle``
    which itself delegates the Beanie write to
    ``ee.cloud.cycles.service.agent_create_cycle`` — single-owner rule
    (Rule 2) holds, and the cycles service already emits
    ``cycle.created`` so the frontend's live activity feed and the
    rail's left list update via the bus.

    Returns the cycles entity's ``CycleResponse`` directly so the
    frontend can ``cycles.unshift(response)`` after the post and avoid
    a re-fetch.
    """
    if "workspace_id" in request.query_params:
        raise CloudError(
            400,
            "cycles.workspace_id_forbidden",
            "workspace_id is taken from auth context, not query",
        )
    return await mc_service.agent_create_cycle(ctx, body)


@router.post(
    "/cycles/{cycle_id}/items/attach",
    response_model=AttachCycleItemsResponse,
)
async def attach_cycle_items(
    cycle_id: str,
    body: AttachCycleItemsRequest,
    ctx: RequestContext = Depends(request_context),
) -> AttachCycleItemsResponse:
    """Attach existing workspace work items to a sprint.

    Powers the Mission Control "+ existing" picker in the sprint header.
    Item ids the caller can't see (wrong workspace, deleted, etc.) are
    reported back in ``skipped`` rather than failing the whole batch,
    so a half-stale selection still partially succeeds.
    """
    return await mc_service.agent_attach_cycle_items(ctx, cycle_id, body)


@router.post(
    "/cycles/{cycle_id}/items/detach",
    response_model=DetachCycleItemsResponse,
)
async def detach_cycle_items(
    cycle_id: str,
    body: DetachCycleItemsRequest,
    ctx: RequestContext = Depends(request_context),
) -> DetachCycleItemsResponse:
    """Detach work items from a sprint.

    Removes items from the sprint by clearing their ``cycle_id``.
    Items the caller can't see are reported back in ``skipped``
    rather than failing the whole batch.
    """
    return await mc_service.agent_detach_cycle_items(ctx, cycle_id, body)


@router.get("/analytics", response_model=AnalyticsResponse)
async def analytics(
    window: str = Query("7d", pattern=r"^(1h|24h|7d)$"),
    ctx: RequestContext = Depends(request_context),
) -> AnalyticsResponse:
    """Compute analytics snapshot for the operator dashboard.

    Returns shipped counts, approval/revert rates, latency percentiles,
    daily breakdown, by-agent, and by-pocket aggregations over the
    requested window (1h | 24h | 7d).
    """
    return await mc_service.agent_analytics(ctx, window=window)


@router.get("/plan-sessions", response_model=PlanSessionListResponse)
async def list_plan_sessions(
    request: Request,
    status: PlanSessionStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(request_context),
) -> PlanSessionListResponse:
    """List the workspace's persisted plan sessions for the Plan tab drafts list.

    Tenancy lives on the auth ctx — passing ``?workspace_id`` is a 400
    rather than a silent leak. ``status`` filters to one drafts-list
    bucket (``draft`` / ``active`` / ``archived``); the service maps to
    the doc-level vocabulary internally.

    Response keys: ``sessions`` (list of plan-session DTOs) + ``total``
    (count of returned sessions). Empty workspaces return
    ``{"sessions": [], "total": 0}`` with HTTP 200.
    """
    if "workspace_id" in request.query_params:
        raise CloudError(
            400,
            "plan_sessions.workspace_id_forbidden",
            "workspace_id is taken from auth context, not query",
        )
    body = ListPlanSessionsRequest(status=status, limit=limit)
    return await mc_service.agent_list_plan_sessions(ctx, body)
