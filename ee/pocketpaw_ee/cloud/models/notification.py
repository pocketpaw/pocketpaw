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


class Notification(TimestampedDocument):
    """In-app notification for a user."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    recipient: Indexed(str)  # type: ignore[valid-type]
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
