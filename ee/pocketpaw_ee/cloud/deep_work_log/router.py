"""FastAPI router for the DeepWorkLog entity.

Mounted under the workspace prefix:

    GET /workspaces/{workspace_id}/deep-work-logs

Read-only, ``audit.read`` permission gate (same as the workspace audit log).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.deps import require_action
from pocketpaw_ee.cloud.deep_work_log import service as deep_work_log_service
from pocketpaw_ee.cloud.deep_work_log.dto import DeepWorkLogPageResponse
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/workspaces",
    tags=["Deep Work Logs"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "/{workspace_id}/deep-work-logs",
    response_model=DeepWorkLogPageResponse,
    dependencies=[Depends(require_action("audit.read"))],
)
async def list_deep_work_logs(
    workspace_id: str,
    action: str | None = Query(default=None, max_length=120),
    actor: str | None = Query(default=None, max_length=120),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> DeepWorkLogPageResponse:
    body = {
        "action": action,
        "actor": actor,
        "since": since,
        "until": until,
        "cursor": cursor,
        "limit": limit,
    }
    return await deep_work_log_service.list_events_response(workspace_id, body)
