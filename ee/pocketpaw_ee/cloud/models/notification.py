"""Notification document."""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import BaseModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class NotificationSource(BaseModel):
    type: str
    id: str
    pocket_id: str | None = None
    room_id: str | None = None
    # The agent this source belongs to, when it belongs to one. The concierge
    # kinds set it: their inbox lives on an AGENT, so without it a client has
    # no id to build a link from and falls back to the chat surface.
    agent_id: str | None = None


class Notification(TimestampedDocument):
    """In-app notification for a user."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    recipient: Indexed(str)  # type: ignore[valid-type]
    actor: str | None = None  # user id of the person who triggered this notification
    type: str  # notification type: mention, comment, reply, invite, agent_complete,
    # pocket_shared, meeting_scheduled, meeting_cancelled, meeting_started, meeting_reminder
    title: str
    body: str = ""
    source: NotificationSource | None = None
    read: bool = False
    expires_at: datetime | None = None

    class Settings:
        name = "notifications"
        indexes = [
            [("recipient", 1), ("read", 1), ("created_at", -1)],
        ]
