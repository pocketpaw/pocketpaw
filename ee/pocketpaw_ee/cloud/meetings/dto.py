# Meetings — request / response schemas.
# Created: 2026-05-19. Every request schema is distinct from every
# response schema (cloud rule §4).

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MeetingProviderName = Literal["google_meet", "zoom"]


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


class CreateMeetingRequest(BaseModel):
    """POST /meetings body."""

    provider: MeetingProviderName
    title: str = Field(min_length=1, max_length=300)
    scheduled_start: datetime | None = None
    duration_minutes: int = Field(default=30, ge=1, le=1440)


class ListMeetingsRequest(BaseModel):
    """Query params for GET /meetings — validated server-side."""

    since: datetime | None = None
    until: datetime | None = None
    status: str | None = None
    provider: MeetingProviderName | None = None
    limit: int = Field(default=50, ge=1, le=200)


class MeetingResponse(BaseModel):
    """Wire shape for one meeting."""

    id: str
    provider: MeetingProviderName
    provider_meeting_id: str
    title: str | None
    join_url: str
    organizer_email: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    actual_start: datetime | None
    actual_end: datetime | None
    status: str
    participants: list[dict[str, Any]] = Field(default_factory=list)
    recording_file_ids: list[str] = Field(default_factory=list)
    transcript_available: bool = False
    created_at: datetime | None = None
    # Recall.ai bot lifecycle status — None until a bot is dispatched.
    bot_status: str | None = None
    bot_status_detail: str | None = None
    bot_status_at: datetime | None = None


class MeetingDetailResponse(MeetingResponse):
    """GET /meetings/{id} — includes the full participants snapshot."""

    raw_provider_payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


class TranscriptResponse(BaseModel):
    """One transcript metadata row. The actual text lives in the file."""

    meeting_id: str
    file_id: str | None
    entry_count: int
    speaker_count: int
    language: str | None
    fetched_at: datetime | None
    indexed_in_kb: bool
