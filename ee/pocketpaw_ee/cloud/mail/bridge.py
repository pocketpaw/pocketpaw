"""Meeting events → email bridge.

Subscribes to meeting.* events on the event bus and sends transactional
emails via the mail service.

Phase 2 scope: meeting.reminder, meeting.cancelled.
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.chat import group_service
from pocketpaw_ee.cloud.mail import send_meeting_scheduled_email
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


async def _on_meeting_scheduled(data: dict[str, Any]) -> None:
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    group_id = data.get("group_id")
    creator_id = data.get("created_by")

    if not (workspace_id and meeting_id):
        return

    title = "Untitled meeting"
    join_url = None
    provider = data.get("provider", "meeting")
    meeting = None
    try:
        meeting = await _MeetingDoc.get(meeting_id)
        if meeting:
            title = meeting.title or title
            join_url = meeting.join_url or None
            provider = meeting.provider or provider
    except Exception:
        logger.exception("Failed to fetch meeting doc %s", meeting_id)

    group_name = "a channel"
    if group_id:
        try:
            group = await _GroupDoc.get(group_id)
            if group:
                group_name = group.name
        except Exception:
            logger.exception("Failed to fetch group doc %s", group_id)

    recipient_ids: list[str] = []
    if group_id:
        try:
            member_ids = await group_service.list_member_ids(group_id)
            recipient_ids = [uid for uid in member_ids if uid != creator_id]
        except Exception:
            logger.exception("Failed to list members for group=%s", group_id)
            return
    elif creator_id:
        recipient_ids = [creator_id]

    if not recipient_ids:
        logger.debug("No recipients for meeting.scheduled %s — skipping email", meeting_id)
        return

    creator_name = "Someone"
    if creator_id:
        try:
            creator = await _UserDoc.get(creator_id)
            if creator:
                creator_name = creator.full_name or creator.email or "Someone"
        except Exception:
            logger.exception("Failed to fetch creator user %s", creator_id)

    scheduled_start_str = ""
    if meeting and meeting.scheduled_start:
        try:
            scheduled_start_str = meeting.scheduled_start.strftime("%a, %b %d at %I:%M %p %Z")
        except Exception:
            scheduled_start_str = str(meeting.scheduled_start)

    for uid in recipient_ids:
        try:
            user = await _UserDoc.get(uid)
            if not user or not user.email:
                logger.debug("Skipping email for user %s — no email found", uid)
                continue
            to_name = user.full_name or user.email
            await send_meeting_scheduled_email(
                to_email=user.email,
                to_name=to_name,
                title=title,
                group_name=group_name,
                scheduled_start=scheduled_start_str,
                join_url=join_url,
                provider=provider,
                creator_name=creator_name,
            )
        except Exception:
            logger.exception("Failed to send meeting scheduled email to user %s", uid)


def register_meeting_mail_listeners() -> None:
    event_bus.subscribe("meeting.scheduled", _on_meeting_scheduled)
    logger.info("registered meeting.* → email subscribers")


__all__ = ["register_meeting_mail_listeners"]
