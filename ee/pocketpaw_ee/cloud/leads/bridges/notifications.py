# ee/pocketpaw_ee/cloud/leads/bridges/notifications.py — ``lead.captured`` →
# owner notifications.
#
# Subscribes to ``lead.captured`` on ``shared.events.event_bus`` and hands each
# event to ``leads.notification_settings.dispatch_site_event``, which routes it
# by the site's own settings: bell + OS push for the workspace owner and admins,
# the full lead by email to the site's confirmed recipients (default: the
# owner's account address), and the site's signed webhook. The workspace
# Slack/webhook config stays the fallback. External sinks fire ONCE per lead,
# not once per admin.
#
# Push recipients are the workspace's owner + admins via
# ``workspace_service.list_admin_ids``; the query filters on the membership's
# workspace, so a lead in workspace A never notifies anyone in workspace B.
#
# The bell row's ``source`` is ``{type: "lead", id: <lead id>, room_id: <site
# id>}``: the frontend maps ``lead`` to ``/sites/<site>?view=leads``. Its body
# names the site by DISPLAY name (site_id is a 24-char hex script name) and
# carries no visitor data, so the lock-screen push stays generic. Email and
# webhook get the lead itself: they carry only the lead id, and the outbox loads
# the lead (name, email, phone, message, properties, site, form_type, source) at
# send time.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.notifications.domain import NotificationSource
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


async def _on_lead_captured(data: dict[str, Any]) -> None:
    """``lead.captured`` → the site's notification sinks.

    A payload missing any of workspace_id / lead_id / site_id is malformed — no
    tenant to scope to, nothing to point at, or no surface to land on — so it
    no-ops rather than minting a dead notification.
    """
    workspace_id = data.get("workspace_id")
    lead_id = data.get("lead_id")
    site_id = data.get("site_id")
    if not (workspace_id and lead_id and site_id):
        logger.warning("lead.captured ignored — incomplete payload keys=%s", sorted(data))
        return

    form_type = data.get("form_type") or "form"
    site_label = str(data.get("site_name") or "").strip() or site_id
    try:
        from pocketpaw_ee.cloud.leads import notification_settings

        await notification_settings.dispatch_site_event(
            workspace_id=workspace_id,
            site_ref=site_id,
            event="lead_captured",
            kind="lead_captured",
            title="New lead",
            body=f"Someone submitted the {form_type} form on {site_label}.",
            source=NotificationSource(type="lead", id=lead_id, room_id=site_id),
            push_recipients=await _workspace_admin_ids(workspace_id),
            lead_id=lead_id,
        )
    except Exception:
        logger.exception("Failed to route lead_captured for lead=%s", lead_id)


async def _workspace_admin_ids(workspace_id: str) -> list[str]:
    """Owner + admin user ids for a workspace. [] on any error — a lookup
    failure must not break the bus for sibling handlers."""
    try:
        from pocketpaw_ee.cloud.workspace import service as workspace_service

        return await workspace_service.list_admin_ids(workspace_id)
    except Exception:
        logger.exception("Failed to list admins for workspace=%s", workspace_id)
        return []


def register_lead_notification_listeners() -> None:
    """Wire the ``lead.captured`` → notification subscriber (from mount_cloud)."""
    event_bus.subscribe("lead.captured", _on_lead_captured)
    logger.info("registered lead.captured → notifications subscriber")


__all__ = ["register_lead_notification_listeners"]
