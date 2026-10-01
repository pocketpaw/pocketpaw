# ee/pocketpaw_ee/cloud/leads/bridges/notifications.py — lead events -> owner
# notifications.
#
# ``lead.captured`` goes to ``leads.notification_settings.dispatch_site_event``,
# which routes it by the site's own settings: bell + OS push for the workspace
# owner and admins, the full lead by email to the site's confirmed recipients
# (default: the owner's account address), and the site's signed webhook, with the
# workspace Slack/webhook config as the fallback. External sinks fire ONCE per
# lead, not once per admin. A lead whose ``source_kind`` is "handoff" is skipped:
# the handoff already notified the owner through its own "handoff" route, and a
# second ping for the same conversation is noise.
#
# ``lead.updated`` (a status change, the owner's own action) goes to the site
# webhook only (``dispatch_lead_updated``), so a CRM behind it stays in sync; no
# bell, no mail.
#
# Push recipients come from ``workspace_service.list_admin_ids``, which filters on
# the membership's workspace, so a lead in workspace A never notifies workspace B.
# The bell row's ``source`` is ``{type: "lead", id: <lead id>, room_id: <site
# id>}`` (the frontend opens ``/sites/<site>?view=leads``); its body names the
# site by display name and carries no visitor data, so lock-screen push stays
# generic. Email and webhook rows carry only the lead id; the outbox loads the
# lead at send time.

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

    if data.get("source_kind") == "handoff":
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


async def _on_lead_updated(data: dict[str, Any]) -> None:
    """``lead.updated`` -> the site webhook (see the module header)."""
    workspace_id = data.get("workspace_id")
    lead_id = data.get("lead_id")
    site_id = data.get("site_id")
    if not (workspace_id and lead_id and site_id):
        logger.warning("lead.updated ignored — incomplete payload keys=%s", sorted(data))
        return
    try:
        from pocketpaw_ee.cloud.leads import notification_settings

        await notification_settings.dispatch_lead_updated(
            workspace_id=workspace_id, site_ref=site_id, lead_id=lead_id
        )
    except Exception:
        logger.exception("Failed to route lead_updated for lead=%s", lead_id)


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
    """Wire the lead event → notification subscribers (from mount_cloud)."""
    event_bus.subscribe("lead.captured", _on_lead_captured)
    event_bus.subscribe("lead.updated", _on_lead_updated)
    logger.info("registered lead.captured / lead.updated → notifications subscribers")


__all__ = ["register_lead_notification_listeners"]
