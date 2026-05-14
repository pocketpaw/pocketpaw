# ee/cloud/mission_control/router.py
# Created: 2026-05-13 (feat/mission-control-facade) — REST surface for the
# Mission Control façade. Endpoints exactly as specified in the audit doc:
# items list, bulk approve/reject, bulk reassign/snooze stubs (501 until
# PR 2), outcomes summary, activity feed.
# Updated: 2026-05-13 (feat/mission-control-cleanup) — lifted the 501s on
# bulk-reassign / bulk-snooze. Both now return a ``BulkActionResponse``
# shape (``affected`` + ``skipped`` + ``bulk_id``) by delegating per-id
# to the Tasks service.
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
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud.license import require_license
from ee.cloud.mission_control import service as mc_service
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
    limit: int = Query(50, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[WorkItemResponse]:
    """Workspace-aware work item feed.

    Filters compose: ``section`` narrows to one pane; ``agent`` and
    ``pocket`` further restrict; ``limit`` caps the projected list.
    """
    body = ListWorkItemsRequest(section=section, agent=agent, pocket=pocket, limit=limit)
    return await mc_service.agent_list_work_items(ctx, body)


@router.post("/items/bulk-approve")
async def bulk_approve(
    body: BulkActionRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Approve N pending Nudges. Returns ``{bulk_id, approved, missing}``."""
    return await mc_service.agent_bulk_approve(ctx, body)


@router.post("/items/bulk-reject")
async def bulk_reject(
    body: BulkActionRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Reject N pending Nudges. Requires non-empty ``reason``.

    Returns ``{bulk_id, rejected, missing}``. The reason text lands on
    every Action's ``rejected_reason`` AND on every audit row's
    ``context.reason`` for soul-bridge replay.
    """
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
