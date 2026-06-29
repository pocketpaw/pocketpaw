"""FastAPI router for the RequestLog entity.

Mounted under the workspace prefix:

    GET /workspaces/{workspace_id}/request-logs

Read-only, ``audit.read`` permission gate (same as the workspace audit log).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.deps import require_action
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.request_log import service as request_log_service
from pocketpaw_ee.cloud.request_log.dto import RequestLogPageResponse

router = APIRouter(
    prefix="/workspaces",
    tags=["Request Logs"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "/{workspace_id}/request-logs",
    response_model=RequestLogPageResponse,
    dependencies=[Depends(require_action("audit.read"))],
)
async def list_request_logs(
    workspace_id: str,
    method: str | None = Query(default=None, max_length=10),
    actor: str | None = Query(default=None, max_length=120),
    min_status: int | None = Query(default=None, ge=100, le=599, alias="minStatus"),
    max_status: int | None = Query(default=None, ge=100, le=599, alias="maxStatus"),
    is_error: bool | None = Query(default=None, alias="isError"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> RequestLogPageResponse:
    body = {
        "method": method,
        "actor": actor,
        "minStatus": min_status,
        "maxStatus": max_status,
        "isError": is_error,
        "since": since,
        "until": until,
        "cursor": cursor,
        "limit": limit,
    }
    return await request_log_service.list_events_response(workspace_id, body)
