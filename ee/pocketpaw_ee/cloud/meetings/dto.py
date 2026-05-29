# Meetings — request / response schemas.
# Created: 2026-05-19. Every request schema is distinct from every
# response schema (cloud rule §4).

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MeetingSourceName = Literal["recall", "livekit"]
MeetingProviderName = Literal["google_meet", "zoom"]


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


class CreateMeetingRequest(BaseModel):
    """POST /meetings body.

    ``source`` selects the platform module that owns the meeting:
      * ``recall``  — external Zoom/Meet call captured by a Recall bot.
        ``provider`` is required (zoom | google_meet).
      * ``livekit`` — native LiveKit room. ``provider`` must be omitted.

    Defaults to ``"recall"`` so existing API consumers (Settings →
    Meetings, the schedule_meeting MCP tool) keep working unchanged.
    """

    source: MeetingSourceName = "recall"
    provider: MeetingProviderName | None = None
    group_id: str | None = Field(default=None, description="Required for livekit — group scope")
    title: str = Field(min_length=1, max_length=300)
    scheduled_start: datetime | None = None
    duration_minutes: int = Field(default=30, ge=1, le=1440)


class ListMeetingsRequest(BaseModel):
    """Query params for GET /meetings — validated server-side."""

    since: datetime | None = None
    until: datetime | None = None
    status: str | None = None
    source: MeetingSourceName | None = None
    provider: MeetingProviderName | None = None
    limit: int = Field(default=50, ge=1, le=200)


class MeetingResponse(BaseModel):
    """Wire shape for one meeting."""

    id: str
    source: MeetingSourceName = "recall"
    provider: MeetingProviderName | None = None
    provider_meeting_id: str
    group_id: str | None = None
    title: str | None
    join_url: str
    organizer_email: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    duration_minutes: int = 30
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
    # True when the meeting was minted by the calendar bridge from a
    # Zoom/Meet URL detected in a calendar event description. Surfaced as
    # a "From calendar" badge so users understand why a meeting they
    # didn't manually schedule appeared in their list.
    auto_created_from_calendar: bool = False
    # When the meeting is linked to a calendar event (via the auto-create
    # path), expose the calendar event id so /calendar can render a small
    # "has recording" indicator next to the event. None for meetings that
    # don't originate from a calendar event.
    calendar_event_id: str | None = None


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


# ---------------------------------------------------------------------------
# Provider credentials (Settings → Meetings connector page)
# ---------------------------------------------------------------------------


class StoreZoomCredentialsRequest(BaseModel):
    """POST /meetings/credentials/zoom — Zoom S2S OAuth app credentials."""

    account_id: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = Field(min_length=1, max_length=500)


class StoreGoogleMeetCredentialsRequest(BaseModel):
    """POST /meetings/credentials/google_meet — Meet OAuth app credentials.

    Stores the app credentials only; the long-lived refresh token is
    obtained afterwards via the OAuth consent callback.
    """

    client_id: str = Field(min_length=1, max_length=300)
    client_secret: str = Field(min_length=1, max_length=300)


class CompleteGoogleMeetOAuthRequest(BaseModel):
    """POST /meetings/credentials/google_meet/callback — the consent result."""

    code: str = Field(min_length=1)
    state: str = Field(min_length=1)


class CredentialsResponse(BaseModel):
    """One provider's credential status. Never carries secret values."""

    provider: MeetingProviderName
    enabled: bool
    has_credentials: bool
    last_validated_at: datetime | None = None
    last_error: str = ""


class GoogleMeetAuthUrlResponse(BaseModel):
    """GET /meetings/credentials/google_meet/auth-url."""

    auth_url: str
    redirect_uri: str


class GoogleMeetRedirectUriResponse(BaseModel):
    """GET /meetings/credentials/google_meet/redirect-uri."""

    redirect_uri: str


class DisconnectResponse(BaseModel):
    """DELETE /meetings/credentials/{provider}."""

    provider: MeetingProviderName
    disconnected: bool


# ---------------------------------------------------------------------------
# Transcription settings
# ---------------------------------------------------------------------------


class MeetingsSettingsResponse(BaseModel):
    """GET / PUT /meetings/settings — the deployment transcription config."""

    transcript_provider: str
    transcript_model: str
    mode: Literal["realtime", "async"]


class UpdateMeetingsSettingsRequest(BaseModel):
    """PUT /meetings/settings body."""

    transcript_provider: str = Field(min_length=1, max_length=80)
    transcript_model: str = Field(default="", max_length=80)
