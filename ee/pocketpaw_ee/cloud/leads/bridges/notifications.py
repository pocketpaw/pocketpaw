# ee/pocketpaw_ee/cloud/leads/bridges/notifications.py — lead capture → in-app
# notification fan-out.
#
# Created 2026-08-06 (feat/coupling-lead-captured, T-6): the second bridge in
# the codebase, built to the shape of ``meetings/bridges/notifications.py``.
# Subscribes to ``lead.captured`` on ``shared.events.event_bus`` and turns each
# one into a notification via ``notifications_service.create``, so a visitor
# submitting a form on a published Paw Site rings the workspace instead of
# waiting for someone to open the Leads view.
#
# Recipients are the workspace's owner + admins, resolved through the existing
# ``workspace_service.list_admin_ids`` (the same resolver the uploads router
# uses) — no new recipient scheme, and it is the tenancy boundary too: the query
# filters on the membership's workspace, so a lead captured in workspace A can
# never notify anyone in workspace B.
#
# The notification ``source`` is ``{type: "lead", id: <lead id>, room_id: <site
# id>}``. ``type`` is the entity kind (not the notification kind) because the
# frontend resolver switches on it: ``core/notifications/target.ts`` maps
# ``lead`` → ``/sites/<site>?view=leads`` and reads the site off ``room_id``
# (its generic navigation-target slot), the same way the meeting bridge passes a
# group there. The notification ``kind`` stays ``lead_captured``.
#
# Updated 2026-08-06 (review fix): the body renders the site's DISPLAY NAME, not
# ``site_id``. site_id is the deploy script name — a 24-char hex id — so the bell
# read "Someone submitted the lead form on 6d4a1f2b3c8e9a0f1b2c3d4e". The emit
# now carries ``site_name`` and the id is only a fallback for an unnamed site.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.notifications.domain import NotificationSource
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _on_lead_captured(data: dict[str, Any]) -> None:
    """``lead.captured`` → one notification per workspace owner/admin.

    A payload missing any of workspace_id / lead_id / site_id is malformed —
    without all three there is no tenant to scope to, nothing to point at, or no
    surface to land on — so it no-ops rather than minting a dead notification.
    """
    workspace_id = data.get("workspace_id")
    lead_id = data.get("lead_id")
    site_id = data.get("site_id")
    if not (workspace_id and lead_id and site_id):
        logger.warning("lead.captured ignored — incomplete payload keys=%s", sorted(data))
        return

    recipients = await _workspace_admin_ids(workspace_id)
    if not recipients:
        return

    form_type = data.get("form_type") or "form"
    # site_id is the deploy script name — a 24-char hex id. Never put it in the
    # body: the owner's bell would read "… on 6d4a1f2b3c8e9a0f1b2c3d4e". Use the
    # display name the emit sends along, and fall back to the id only for a site
    # that was never named (where the id is at least an identifier they can match
    # against the URL they land on).
    site_label = str(data.get("site_name") or "").strip() or site_id
    for recipient in recipients:
        await _create(
            workspace_id=workspace_id,
            recipient=recipient,
            lead_id=lead_id,
            site_id=site_id,
            body=f"Someone submitted the {form_type} form on {site_label}.",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _workspace_admin_ids(workspace_id: str) -> list[str]:
    """Owner + admin user ids for a workspace. Returns [] on any error — a
    lookup failure must not break the bus for sibling handlers."""
    try:
        from pocketpaw_ee.cloud.workspace import service as workspace_service

        return await workspace_service.list_admin_ids(workspace_id)
    except Exception:
        logger.exception("Failed to list admins for workspace=%s", workspace_id)
        return []


async def _create(
    *,
    workspace_id: str,
    recipient: str,
    lead_id: str,
    site_id: str,
    body: str,
) -> None:
    """Wrap notifications_service.create — late import to avoid circular deps
    and tolerate the service being unavailable in unit-test contexts."""
    try:
        from pocketpaw_ee.cloud.notifications import service as notifications_service

        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind="lead_captured",
            title="New lead",
            body=body,
            source=NotificationSource(type="lead", id=lead_id, room_id=site_id),
        )
    except Exception:
        logger.exception("Failed to create lead_captured notification for lead=%s", lead_id)


# ---------------------------------------------------------------------------
# Registration — called from mount_cloud() after init_realtime.
# ---------------------------------------------------------------------------


def register_lead_notification_listeners() -> None:
    """Wire the ``lead.captured`` → notification subscriber."""
    event_bus.subscribe("lead.captured", _on_lead_captured)
    logger.info("registered lead.captured → notifications subscriber")


__all__ = ["register_lead_notification_listeners"]
