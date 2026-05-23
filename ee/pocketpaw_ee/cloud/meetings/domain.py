# Meetings — domain value objects.
# Created: 2026-05-19. Frozen dataclasses constructed from Beanie docs in
# service.py. Tenancy is required at construction (workspace_id has no
# default) per the ee/cloud rule §3.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

MeetingSource = Literal["recall", "livekit"]
MeetingProvider = Literal["google_meet", "zoom"]
MeetingStatus = Literal[
    "scheduled",
    "in_progress",
    "ended",
    "transcript_ready",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class Meeting:
    """One meeting in one workspace.

    The wire layer (``dto.MeetingResponse``) and the persistence layer
    (``models.meeting.Meeting``) each have their own shape; this is the
    canonical in-memory view consumed by tools, listeners, and tests.

    ``source`` selects the implementing provider:
      * ``recall``  — external Zoom/Meet/Teams meeting; ``provider``
        names which (zoom/google_meet) and a Recall.ai bot captures it.
      * ``livekit`` — native real-time room on our LiveKit Cloud;
        ``provider`` is unset.
    """

    id: str
    workspace_id: str
    source: MeetingSource
    # Recall-specific. None for source="livekit".
    provider: MeetingProvider | None
    provider_meeting_id: str
    provider_space_id: str | None
    title: str | None
    join_url: str
    organizer_email: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    actual_start: datetime | None
    actual_end: datetime | None
    status: MeetingStatus
    participants: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    recording_file_ids: tuple[str, ...] = field(default_factory=tuple)
    created_by_user_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class MeetingTranscript:
    """One transcript session for one meeting.

    ``file_id`` references the stored ``.vtt``/``.txt`` blob in the
    uploads pipeline. ``None`` means the transcript row exists but the
    blob hasn't been fetched yet (in-flight or polling-fallback pending).
    """

    id: str
    workspace_id: str
    meeting_id: str
    provider_transcript_id: str
    file_id: str | None
    entry_count: int
    speaker_count: int
    language: str | None
    fetched_at: datetime | None
    indexed_in_kb: bool
