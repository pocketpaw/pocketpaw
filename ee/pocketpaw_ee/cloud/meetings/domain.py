"""Domain value objects for the meetings module.

Pure-Python frozen dataclasses.  No Beanie, no Pydantic, no FastAPI
imports.  The repository layer converts between these and the Beanie
``MeetingSchedule`` document; the DTO layer converts these to Pydantic
response models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

MeetingStatus = Literal["scheduled", "active", "ended", "cancelled"]


@dataclass(frozen=True)
class MeetingSchedule:
    """A scheduled group meeting."""

    id: str
    workspace_id: str
    group_id: str
    created_by: str  # user_id
    scheduled_at: datetime
    duration_minutes: int
    agenda: str
    status: MeetingStatus
    livekit_room_name: str | None  # set when meeting becomes active
    created_at: datetime
    updated_at: datetime


__all__ = ["MeetingSchedule", "MeetingStatus"]
