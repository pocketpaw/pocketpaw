# Web Push REST router (pocketpaw#1391).
# Created: 2026-06-09 (feat/push-subscription-store) — thin boundary: parses
# requests, delegates to ``ee.cloud.push.service``, returns DTOs. Mounted at
# ``/api/v1`` so routes land at ``/api/v1/push/*``.
# Updated: 2026-06-09 (review nits) — the subscribe handler reads the
# ``User-Agent`` request header and passes it to the service, so the stored
# user-agent is captured server-side rather than trusted from the client body.
#
#   GET  /push/vapid-public-key  → {"key": "<base64url public key>"}
#   POST /push/subscribe         → upsert subscription (idempotent on endpoint)
#   POST /push/unsubscribe         → remove subscription by endpoint
#
# All routes require an authenticated request context; the workspace scopes
# the VAPID keypair and the subscription rows. The VAPID *private* key is
# never exposed by any route here.

from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.push import service as push_service
from pocketpaw_ee.cloud.push.dto import (
    SubscribeRequest,
    SubscriptionResponse,
    UnsubscribeRequest,
    VapidPublicKeyResponse,
    subscription_to_dto,
)

router = APIRouter(prefix="/push", tags=["Push"])


def _require_workspace(ctx: RequestContext) -> str:
    if not ctx.workspace_id:
        raise Forbidden("push.workspace_required", "An active workspace is required")
    return ctx.workspace_id


@router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
async def vapid_public_key(
    ctx: RequestContext = Depends(request_context),
) -> VapidPublicKeyResponse:
    """Return the workspace's VAPID public key the browser subscribes with."""
    workspace_id = _require_workspace(ctx)
    key = await push_service.get_vapid_public_key(workspace_id)
    return VapidPublicKeyResponse(key=key)


@router.post("/subscribe", response_model=SubscriptionResponse)
async def subscribe(
    body: SubscribeRequest,
    ctx: RequestContext = Depends(request_context),
    user_agent: str = Header(default=""),
) -> SubscriptionResponse:
    """Persist (upsert) the caller's browser Web Push subscription.

    ``user_agent`` is captured from the request header (FastAPI maps the
    ``user_agent`` parameter to the ``User-Agent`` header) rather than read
    from the request body — the client doesn't get to spoof it.
    """
    workspace_id = _require_workspace(ctx)
    sub = await push_service.subscribe(workspace_id, ctx.user_id, body, user_agent=user_agent)
    return subscription_to_dto(sub)


@router.post("/unsubscribe")
async def unsubscribe(
    body: UnsubscribeRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Remove a subscription by endpoint within the caller's workspace."""
    workspace_id = _require_workspace(ctx)
    removed = await push_service.unsubscribe(workspace_id, ctx.user_id, body)
    return {"removed": removed}
