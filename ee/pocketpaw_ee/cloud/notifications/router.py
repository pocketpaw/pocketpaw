"""Notifications REST router.

Thin: parses requests, delegates to ``ee.cloud.notifications.service``,
returns ``NotificationOut`` DTOs at the boundary. FastAPI serializes to
JSON. The wire shape matches the legacy ``_to_wire`` output byte-for-byte
(verified by ``tests/cloud/notifications/test_router_golden.py``).

Updated: 2026-07-08 (feat/external-alerting-delivery) — added the
external-delivery config surface: ``GET`` / ``PUT /notifications/delivery-config``
(workspace-scoped, ``notifications.manage`` = ADMIN) so a workspace admin can
paste a Slack incoming-webhook / generic HTTPS webhook URL. The router never
touches the Beanie doc — it delegates to the service (sole writer).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import (
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud.notifications import service as notifications_service
from pocketpaw_ee.cloud.notifications.dto import NotificationOut, notification_to_dto

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class DeliveryConfigRequest(BaseModel):
    """Body for ``PUT /notifications/delivery-config``.

    Full replacement of the workspace's external-delivery config. An empty /
    omitted URL clears that sink. ``enabled`` is the master switch. ``routes``
    optionally narrows a notification kind to specific sinks (``"slack"`` /
    ``"webhook"``); the default (empty) delivers every kind to every configured
    sink. URLs are validated (https + public host) by the service; a bad URL
    yields a ``notifications.invalid_webhook_url`` error.
    """

    slack_webhook_url: str | None = Field(default=None)
    webhook_url: str | None = Field(default=None)
    enabled: bool = Field(default=False)
    routes: dict[str, list[str]] = Field(default_factory=dict)


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(request_context),
) -> list[NotificationOut]:
    notes = await notifications_service.list_for_user(ctx.user_id, unread=unread, limit=limit)
    return [notification_to_dto(n) for n in notes]


@router.get("/unread-count")
async def unread_count(
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Return the unread notification count for the current user."""
    count = await notifications_service.count_unread(ctx.user_id)
    return {"count": count}


@router.post("/{notification_id}/read")
@router.patch("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    await notifications_service.mark_read(notification_id, ctx.user_id)
    return {"ok": True}


@router.post("/read-all")
async def read_all(
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Mark all notifications as read for the current user."""
    count = await notifications_service.clear_all(ctx.user_id)
    return {"cleared": count}


@router.post("/clear")
async def clear_all(
    ctx: RequestContext = Depends(request_context),
) -> dict:
    count = await notifications_service.clear_all(ctx.user_id)
    return {"cleared": count}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    ok = await notifications_service.delete_notification(notification_id, ctx.user_id)
    if not ok:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# External-delivery config (Criterion 1: get alerts OUT of the app).
# Workspace-scoped, ADMIN-gated (``notifications.manage``): the config controls
# where the server POSTs on every notification, so it extends the workspace's
# egress surface — same tier as connector.manage / belt.manage.
# ---------------------------------------------------------------------------


@router.get("/delivery-config")
async def get_delivery_config(
    _user=Depends(require_action_any_workspace("notifications.manage")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Return the workspace's external-delivery config, or an empty default when
    unset (so the settings form always has a shape to render)."""
    config = await notifications_service.get_delivery_config(workspace_id)
    if config is None:
        return {
            "workspace_id": workspace_id,
            "slack_webhook_url": None,
            "webhook_url": None,
            "enabled": False,
            "routes": {},
        }
    return config


@router.put("/delivery-config")
async def put_delivery_config(
    body: DeliveryConfigRequest,
    _user=Depends(require_action_any_workspace("notifications.manage")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Upsert the workspace's external-delivery config. ADMIN-only."""
    return await notifications_service.set_delivery_config(
        workspace_id,
        slack_webhook_url=body.slack_webhook_url,
        webhook_url=body.webhook_url,
        enabled=body.enabled,
        routes=body.routes,
    )
