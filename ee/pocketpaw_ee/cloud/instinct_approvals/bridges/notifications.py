# ee/pocketpaw_ee/cloud/instinct_approvals/bridges/notifications.py —
# template-level approval requested → in-app notification fan-out.
#
# Created 2026-08-06 (feat/coupling-template-approvals, T-5), built to the shape
# of ``meetings/bridges/notifications.py``. Subscribes to
# ``instinct.approval.created`` on ``shared.events.event_bus`` and turns each
# one into a PERSISTED notification via ``notifications_service.create``.
#
# Why it exists: creating a template-level approval already emitted a realtime
# event, but that is a websocket fan-out — it reaches whoever happens to have a
# socket open and nobody else. The decision it is asking for authorises a whole
# CLASS of future writes, so an owner who was offline at that moment learned
# nothing and the write sat parked indefinitely. A notification row survives the
# owner being away.
#
# Recipients are the workspace's owner + admins via the existing
# ``workspace_service.list_admin_ids`` (the resolver the uploads router and the
# realtime AudienceResolver already use) — no new recipient scheme. It is the
# tenancy boundary too: the query filters memberships by workspace, so an
# approval raised in workspace A can never notify anyone in workspace B.
#
# The notification ``source`` is ``{type: "instinct_action", id: <approval id>,
# pocket_id: <pocket>}``. ``type`` is the ENTITY kind, not the notification
# kind — the frontend's navigation resolver switches on it — while the
# notification ``kind`` stays ``approval_pending``.
#
# Nothing here may break the approval flow: the recipient lookup and each
# ``create`` are individually wrapped, and ``event_bus.emit`` guards handlers on
# top of that. Worst case an approval lands with no notification, which is the
# behaviour that existed before this bridge.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.instinct_approvals.service import CREATED_TOPIC
from pocketpaw_ee.cloud.notifications.domain import NotificationSource
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _on_instinct_approval_created(data: dict[str, Any]) -> None:
    """``instinct.approval.created`` → one notification per workspace owner/admin.

    A payload missing the workspace or the approval id is malformed — without
    the workspace there is no tenant to scope the recipient lookup to, and
    without the id the notification points at nothing — so it no-ops rather
    than minting a dead notification or, worse, a workspace-less one.
    """
    workspace_id = data.get("workspace_id")
    approval_id = data.get("id")
    if not (workspace_id and approval_id):
        logger.warning(
            "instinct.approval.created ignored — incomplete payload keys=%s", sorted(data)
        )
        return

    recipients = await _workspace_admin_ids(str(workspace_id))
    if not recipients:
        return

    action_name = data.get("action_name") or "an action"
    pocket_id = data.get("pocket_id") or None
    row_id = data.get("row_id") or ""
    target = f"row {row_id}" if row_id else "a set of rows"
    body = f"{action_name} on {target} needs approval before it can run."

    for recipient in recipients:
        await _create(
            workspace_id=str(workspace_id),
            recipient=recipient,
            approval_id=str(approval_id),
            pocket_id=str(pocket_id) if pocket_id else None,
            body=body,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _workspace_admin_ids(workspace_id: str) -> list[str]:
    """Owner + admin user ids for a workspace. Returns [] on any error — a
    lookup failure must not break the approval that triggered it."""
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
    approval_id: str,
    pocket_id: str | None,
    body: str,
) -> None:
    """Wrap notifications_service.create — late import to avoid circular deps
    and tolerate the service being unavailable in unit-test contexts.

    Per-recipient, deliberately: one recipient's delivery blowing up must not
    cost the other admins their notification.
    """
    try:
        from pocketpaw_ee.cloud.notifications import service as notifications_service

        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind="approval_pending",
            title="Approval needed",
            body=body,
            source=NotificationSource(type="instinct_action", id=approval_id, pocket_id=pocket_id),
        )
    except Exception:
        logger.exception(
            "Failed to create approval_pending notification for approval=%s", approval_id
        )


# ---------------------------------------------------------------------------
# Registration — called from mount_cloud() after init_realtime.
# ---------------------------------------------------------------------------


def register_instinct_approval_notification_listeners() -> None:
    """Wire the ``instinct.approval.created`` → notification subscriber."""
    event_bus.subscribe(CREATED_TOPIC, _on_instinct_approval_created)
    logger.info("registered %s → notifications subscriber", CREATED_TOPIC)


__all__ = ["register_instinct_approval_notification_listeners"]
