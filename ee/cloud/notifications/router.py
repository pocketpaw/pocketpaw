"""Notifications REST router.

Thin: parses requests, delegates to ``ee.cloud.notifications.service``,
returns ``NotificationOut`` DTOs at the boundary. FastAPI serializes to
JSON. The wire shape matches the legacy ``_to_wire`` output byte-for-byte
(verified by ``tests/cloud/notifications/test_router_golden.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud.notifications import service as notifications_service
from ee.cloud.notifications.dto import NotificationOut, notification_to_dto

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(request_context),
) -> list[NotificationOut]:
    notes = await notifications_service.list_for_user(ctx.user_id, unread=unread, limit=limit)
    return [notification_to_dto(n) for n in notes]


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    await notifications_service.mark_read(notification_id, ctx.user_id)
    return {"ok": True}


@router.post("/clear")
async def clear_all(
    ctx: RequestContext = Depends(request_context),
) -> dict:
    count = await notifications_service.clear_all(ctx.user_id)
    return {"cleared": count}
