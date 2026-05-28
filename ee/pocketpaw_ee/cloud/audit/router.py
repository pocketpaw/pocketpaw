# router.py — FastAPI router for the Audit entity.
# Created: 2026-05-17 — Workspace-scoped /api/v1/audit. Never raises
#   HTTPException — CloudError → JSON via _core.http.
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks
from pocketpaw_ee.cloud.audit.dto import (
    AuditListResponse,
    AuditPageResponse,
    AuditQueryRequest,
    AuditWebhookOut,
    CreateAuditWebhookRequest,
    ListAuditRequest,
    RotatedSecretResponse,
    UpdateAuditWebhookRequest,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook as _AuditWebhookDoc
from pocketpaw_ee.cloud.shared.deps import require_action_any_workspace

router = APIRouter(
    prefix="/audit",
    tags=["Audit"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "",
    response_model=AuditListResponse,
    dependencies=[Depends(require_action_any_workspace("audit.read"))],
)
async def list_audit(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None),
    pocket_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    ctx: RequestContext = Depends(request_context),
) -> AuditListResponse:
    if "workspace_id" in request.query_params:
        raise CloudError(
            400,
            "audit.workspace_id_forbidden",
            "workspace_id is taken from auth context, not query",
        )
    body = ListAuditRequest(
        q=q,
        category=category,  # type: ignore[arg-type]
        pocket_id=pocket_id,
        actor=actor,
        limit=limit,
        cursor=cursor,
    )
    return await audit_service.agent_list_audit(ctx, body)


# ---------------------------------------------------------------------------
# Workspace-scoped audit log (Wave 2 Task 10).
#
# Separate router mounted under ``/workspaces/{workspace_id}/audit`` so the
# admin-only ``audit.read`` guard binds to the path workspace, not the
# active workspace on the user record.
# ---------------------------------------------------------------------------


workspace_router = APIRouter(
    prefix="/workspaces",
    tags=["Audit"],
    dependencies=[Depends(require_license)],
)


@workspace_router.get(
    "/{workspace_id}/audit",
    response_model=AuditPageResponse,
    dependencies=[Depends(require_action("audit.read"))],
)
async def list_workspace_audit(
    workspace_id: str,
    action: str | None = Query(default=None, max_length=120),
    actor: str | None = Query(default=None, max_length=120),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> AuditPageResponse:
    body = AuditQueryRequest(
        action=action,
        actor=actor,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
    )
    return await audit_service.list_events_response(workspace_id, body)


# ---------------------------------------------------------------------------
# Wave 3 Task 15 — CSV export + SIEM webhooks.
# ---------------------------------------------------------------------------


def _webhook_to_wire(doc: _AuditWebhookDoc, *, secret: str | None = None) -> AuditWebhookOut:
    return AuditWebhookOut(
        id=str(doc.id),
        workspaceId=doc.workspace,
        url=doc.url,
        enabled=doc.enabled,
        failureCount=doc.failure_count,
        lastDeliveryAt=doc.last_delivery_at.isoformat() if doc.last_delivery_at else None,
        lastStatus=doc.last_status,
        lastError=doc.last_error,
        createdBy=doc.created_by,
        createdAt=doc.created_at.isoformat(),
        secret=secret,
    )


@workspace_router.get(
    "/{workspace_id}/audit/export",
    dependencies=[Depends(require_action("audit.read"))],
)
async def export_workspace_audit(
    workspace_id: str,
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> StreamingResponse:
    filename = f"audit-{workspace_id}-{since or 'all'}-{until or 'all'}.csv"
    gen = audit_service.stream_export_csv(workspace_id, since=since, until=until)
    return StreamingResponse(
        gen,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@workspace_router.post(
    "/{workspace_id}/audit/webhooks",
    response_model=AuditWebhookOut,
    dependencies=[Depends(require_action("workspace.update"))],
)
async def create_audit_webhook(
    workspace_id: str,
    body: CreateAuditWebhookRequest,
    ctx: RequestContext = Depends(request_context),
) -> AuditWebhookOut:
    doc, secret = await audit_webhooks.create_webhook(
        workspace_id, body.url, created_by=ctx.user_id or "system"
    )
    return _webhook_to_wire(doc, secret=secret)


@workspace_router.get(
    "/{workspace_id}/audit/webhooks",
    response_model=list[AuditWebhookOut],
    dependencies=[Depends(require_action("workspace.update"))],
)
async def list_audit_webhooks(workspace_id: str) -> list[AuditWebhookOut]:
    docs = await audit_webhooks.list_webhooks(workspace_id)
    return [_webhook_to_wire(d) for d in docs]


@workspace_router.patch(
    "/{workspace_id}/audit/webhooks/{webhook_id}",
    response_model=AuditWebhookOut,
    dependencies=[Depends(require_action("workspace.update"))],
)
async def update_audit_webhook(
    workspace_id: str,
    webhook_id: str,
    body: UpdateAuditWebhookRequest,
) -> AuditWebhookOut:
    doc = await audit_webhooks.update_webhook(workspace_id, webhook_id, enabled=body.enabled)
    return _webhook_to_wire(doc)


@workspace_router.post(
    "/{workspace_id}/audit/webhooks/{webhook_id}/rotate",
    response_model=RotatedSecretResponse,
    dependencies=[Depends(require_action("workspace.update"))],
)
async def rotate_audit_webhook_secret(
    workspace_id: str,
    webhook_id: str,
) -> RotatedSecretResponse:
    doc, secret = await audit_webhooks.rotate_secret(workspace_id, webhook_id)
    return RotatedSecretResponse(webhook=_webhook_to_wire(doc, secret=secret), secret=secret)


@workspace_router.delete(
    "/{workspace_id}/audit/webhooks/{webhook_id}",
    dependencies=[Depends(require_action("workspace.update"))],
)
async def delete_audit_webhook(workspace_id: str, webhook_id: str) -> dict:
    await audit_webhooks.delete_webhook(workspace_id, webhook_id)
    return {"ok": True}
