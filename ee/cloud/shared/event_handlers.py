"""Cross-domain event handlers.

Registered on app startup via register_event_handlers().
Handles side effects that span domain boundaries.
"""

from __future__ import annotations

import logging

from ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


async def _on_invite_accepted(data: dict) -> None:
    """Auto-add user to group when accepting an invite with group_id."""
    group_id = data.get("group_id")
    user_id = data.get("user_id")
    workspace_id = data.get("workspace_id")

    if not group_id or not user_id:
        return

    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group

    try:
        group = await Group.get(PydanticObjectId(group_id))
        if group and user_id not in group.members:
            group.members.append(user_id)
            await group.save()
            logger.info("Auto-added user %s to group %s on invite accept", user_id, group_id)
    except Exception:
        logger.exception("Failed to auto-add user to group on invite accept")

    # Create notification
    await _create_notification(
        workspace_id=workspace_id or "",
        recipient=user_id,
        type="invite",
        title="Invite accepted",
        body="You joined workspace",
    )


async def _on_message_sent(data: dict) -> None:
    """Create notifications for mentioned users, update group stats."""
    group_id = data.get("group_id")
    sender_id = data.get("sender_id")
    mentions = data.get("mentions", [])

    if not group_id:
        return

    # Update group stats (last_message_at, message_count)
    from datetime import UTC, datetime

    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group

    try:
        group = await Group.get(PydanticObjectId(group_id))
        if group:
            group.last_message_at = datetime.now(UTC)
            group.message_count += 1
            await group.save()
    except Exception:
        logger.exception("Failed to update group stats on message sent")

    # Create notifications for mentioned users
    workspace_id = data.get("workspace_id", "")
    for mention in mentions:
        if mention.get("type") == "user" and mention.get("id") != sender_id:
            await _create_notification(
                workspace_id=workspace_id,
                recipient=mention["id"],
                type="mention",
                title="You were mentioned",
                body=data.get("content", "")[:100],
                source_type="group",
                source_id=group_id,
            )


async def _on_pocket_shared(data: dict) -> None:
    """Notify user when a pocket is shared with them."""
    recipient = data.get("target_user_id")
    pocket_id = data.get("pocket_id")
    workspace_id = data.get("workspace_id", "")

    if not recipient or not pocket_id:
        return

    await _create_notification(
        workspace_id=workspace_id,
        recipient=recipient,
        type="pocket_shared",
        title="Pocket shared with you",
        body=data.get("pocket_name", ""),
        source_type="pocket",
        source_id=pocket_id,
        pocket_id=pocket_id,
    )


async def _on_member_removed(data: dict) -> None:
    """Clean up group memberships when member is removed from workspace."""
    workspace_id = data.get("workspace_id")
    user_id = data.get("user_id")

    if not workspace_id or not user_id:
        return

    from ee.cloud.models.group import Group

    # Remove from all groups in workspace
    groups = await Group.find(
        Group.workspace == workspace_id,
        {"members": user_id},
    ).to_list()

    for group in groups:
        if user_id in group.members:
            group.members.remove(user_id)
            await group.save()
            logger.info("Removed user %s from group %s (workspace removal)", user_id, str(group.id))

    # Revoke pocket access
    from ee.cloud.models.pocket import Pocket

    pockets = await Pocket.find(
        Pocket.workspace == workspace_id,
        {"shared_with": user_id},
    ).to_list()

    for pocket in pockets:
        if user_id in pocket.shared_with:
            pocket.shared_with.remove(user_id)
            await pocket.save()


async def _create_notification(
    workspace_id: str,
    recipient: str,
    type: str,
    title: str,
    body: str = "",
    source_type: str | None = None,
    source_id: str | None = None,
    pocket_id: str | None = None,
) -> None:
    """Create a notification document."""
    from ee.cloud.models.notification import Notification, NotificationSource

    try:
        source = None
        if source_type and source_id:
            source = NotificationSource(type=source_type, id=source_id, pocket_id=pocket_id)

        notif = Notification(
            workspace=workspace_id,
            recipient=recipient,
            type=type,
            title=title,
            body=body,
            source=source,
        )
        await notif.insert()
    except Exception:
        logger.exception("Failed to create notification")


def register_event_handlers() -> None:
    """Wire up all cross-domain event handlers."""
    event_bus.subscribe("invite.accepted", _on_invite_accepted)
    event_bus.subscribe("message.sent", _on_message_sent)
    event_bus.subscribe("pocket.shared", _on_pocket_shared)
    event_bus.subscribe("member.removed", _on_member_removed)
    logger.info("Cloud event handlers registered")
