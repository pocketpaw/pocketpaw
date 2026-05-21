"""MeetingSchedule document — persisted scheduled group meetings."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument

MeetingStatus = Literal["scheduled", "active", "ended", "cancelled"]


class MeetingSchedule(TimestampedDocument):
    """A scheduled group meeting."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    group_id: str
    created_by: str  # user_id
    scheduled_at: datetime
    duration_minutes: int = 30
    agenda: str = ""
    status: MeetingStatus = "scheduled"
    livekit_room_name: str | None = None

    class Settings:
        name = "meeting_schedules"
        use_state_management = True
        indexes = [
            [("group_id", 1), ("scheduled_at", 1)],
            [("workspace", 1), ("status", 1)],
        ]
