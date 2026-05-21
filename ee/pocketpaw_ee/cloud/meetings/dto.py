"""Wire-format DTOs for the meetings module.

``MeetingScheduleOut`` is the response shape clients see.
``CreateMeetingRequest`` / ``UpdateMeetingRequest`` are the request shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.meetings.domain import MeetingSchedule, MeetingStatus

# ---- Request schemas ----


class CreateMeetingRequest(BaseModel):
    group_id: str = Field(..., description="Group to schedule the meeting in")
    scheduled_at: datetime = Field(..., description="ISO datetime for the meeting start")
    duration_minutes: int = Field(default=30, ge=5, le=480, description="Duration in minutes")
    agenda: str = Field(default="", max_length=2000, description="Meeting agenda / description")


class UpdateMeetingRequest(BaseModel):
    scheduled_at: datetime | None = Field(None, description="New ISO datetime")
    duration_minutes: int | None = Field(None, ge=5, le=480, description="New duration")
    agenda: str | None = Field(None, max_length=2000, description="New agenda")
    status: MeetingStatus | None = Field(None, description="New status")


# ---- Response schemas ----


class MeetingScheduleOut(BaseModel):
    id: str
    workspace_id: str
    group_id: str
    created_by: str
    scheduled_at: datetime
    duration_minutes: int
    agenda: str
    status: MeetingStatus
    livekit_room_name: str | None
    created_at: datetime | None
    updated_at: datetime | None


def meeting_to_dto(m: MeetingSchedule) -> MeetingScheduleOut:
    """Map a domain ``MeetingSchedule`` to its wire DTO.

    Ensures ``scheduled_at`` is timezone-aware (UTC) so JSON serialization
    includes the ``+00:00`` suffix — without it, JavaScript interprets the
    naive ISO string as local time, causing date shifts.
    """
    return MeetingScheduleOut(
        id=m.id,
        workspace_id=m.workspace_id,
        group_id=m.group_id,
        created_by=m.created_by,
        scheduled_at=(
            m.scheduled_at.replace(tzinfo=UTC) if m.scheduled_at.tzinfo is None else m.scheduled_at
        ),
        duration_minutes=m.duration_minutes,
        agenda=m.agenda,
        status=m.status,
        livekit_room_name=m.livekit_room_name,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


__all__ = [
    "CreateMeetingRequest",
    "UpdateMeetingRequest",
    "MeetingScheduleOut",
    "meeting_to_dto",
]
