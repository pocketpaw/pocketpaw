# ee/pocketpaw_ee/cloud/leads/notifications_router.py
# HTTP surface for per-site owner notifications (``Site.lead_notifications``).
# Thin: every route delegates to ``leads.notification_settings``.
#
# Owner/admin routes, workspace-scoped through the caller's active workspace and
# gated on ``notifications.manage`` (ADMIN): a member gets 403, and a site in
# another workspace is a 404 (the service only loads sites in the caller's
# workspace):
#   GET    /sites/{site_id}/lead-notifications
#   PUT    /sites/{site_id}/lead-notifications              (include_owner, events, webhook)
#   POST   /sites/{site_id}/lead-notifications/recipients   (queues a confirm email)
#   DELETE /sites/{site_id}/lead-notifications/recipients/{email}
#   POST   /sites/{site_id}/lead-notifications/test
#
# One PUBLIC route: GET /lead-notifications/confirm/{token}, the link in the
# confirm email. The token is a 7-day Fernet token, so no session is needed;
# it answers a small HTML page and is idempotent. The token rides in the PATH,
# not a ``?token=`` query, because the dashboard auth middleware treats a
# ``token`` query parameter as a dashboard credential.

from __future__ import annotations

import html

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_action_any_workspace
from pocketpaw_ee.cloud.leads import notification_settings as settings_service

router = APIRouter(tags=["Sites"])

_MANAGE = require_action_any_workspace("notifications.manage")


class LeadNotificationsUpdate(BaseModel):
    """Partial update; omitted fields are left as they are."""

    include_owner: bool | None = None
    events: dict[str, list[str]] | None = None
    # A new URL mints a signing secret, returned once as ``webhook_secret``.
    webhook_url: str | None = Field(default=None, max_length=2048)
    clear_webhook: bool = False


class RecipientAdd(BaseModel):
    email: str = Field(max_length=320)


@router.get("/sites/{site_id}/lead-notifications")
async def get_lead_notifications(
    site_id: str,
    _user=Depends(_MANAGE),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return await settings_service.get_settings(workspace_id, site_id)


@router.put("/sites/{site_id}/lead-notifications")
async def put_lead_notifications(
    site_id: str,
    body: LeadNotificationsUpdate,
    _user=Depends(_MANAGE),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return await settings_service.update_settings(
        workspace_id,
        site_id,
        include_owner=body.include_owner,
        events=body.events,
        webhook_url=body.webhook_url,
        clear_webhook=body.clear_webhook,
    )


@router.post("/sites/{site_id}/lead-notifications/recipients")
async def add_lead_notification_recipient(
    site_id: str,
    body: RecipientAdd,
    user=Depends(_MANAGE),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return await settings_service.add_recipient(
        workspace_id, site_id, body.email, added_by=str(getattr(user, "id", "") or "")
    )


@router.delete("/sites/{site_id}/lead-notifications/recipients/{email}")
async def remove_lead_notification_recipient(
    site_id: str,
    email: str,
    _user=Depends(_MANAGE),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return await settings_service.remove_recipient(workspace_id, site_id, email)


@router.post("/sites/{site_id}/lead-notifications/test")
async def send_lead_notification_test(
    site_id: str,
    _user=Depends(_MANAGE),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return await settings_service.send_test(workspace_id, site_id)


def _page(title: str, message: str, status_code: int) -> HTMLResponse:
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title></head>"
        '<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:480px;margin:64px auto;padding:0 16px;line-height:1.5;color:#111827">'
        f'<h1 style="font-size:22px">{html.escape(title)}</h1>'
        f"<p>{html.escape(message)}</p></body></html>"
    )
    return HTMLResponse(
        body,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/lead-notifications/confirm/{token}", response_class=HTMLResponse)
async def confirm_lead_notification_email(token: str) -> HTMLResponse:
    state, site_name = await settings_service.confirm(token)
    if state == "confirmed":
        where = f" for {site_name}" if site_name else ""
        return _page("Email confirmed", f"You'll now get new-lead email{where}.", 200)
    return _page(
        "Link not valid",
        "This confirm link has expired or was replaced. Ask the site owner to add you again.",
        400,
    )
