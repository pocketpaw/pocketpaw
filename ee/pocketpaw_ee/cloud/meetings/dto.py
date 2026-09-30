# Meetings — request / response schemas.
# Created: 2026-05-19. Every request schema is distinct from every
# response schema (cloud rule §4).
#
# 2026-10-01 (feat/meetings-instant, MC-1): ``StartInstantMeetingRequest`` for
# ``POST /meetings/instant``; ``MeetingResponse`` gains ``code`` (display form
# ``xxx-xxxx-xxx``), ``link`` (``<app>/m/<code>``), ``room_group_id``,
# ``access``, ``host_user_id`` and ``description``. ``guest_emails`` is kept
# off the wire for now (nothing writes it yet; the host-only view comes with
# the invite slice).
#
# 2026-10-01 (feat/meetings-by-code, MC-2): ``MeetingLookupResponse`` (the
# public ``GET /meetings/by-code/{code}`` shape — six fields, nothing that
# identifies a person, room or workspace) and ``JoinMeetingByCodeResponse``.
#
# 2026-10-01 (feat/meetings-lobby, MC-3): the lobby shapes. Guest side (public):
# ``KnockRequest`` → ``KnockCreatedResponse`` {knock_id, secret, status};
# ``KnockStatusResponse`` {status} plus {token, room_name, identity,
# livekit_url} once admitted and the call is running (routes drop the None
# keys). Member side: ``KnockSummaryResponse`` and ``KnockDecisionResponse``.
# ``UpdateMeetingRequest`` backs PATCH /meetings/{id} (host only; access, title,
# description; any other field is refused, not ignored).

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer

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


class StartInstantMeetingRequest(BaseModel):
    """POST /meetings/instant body. Both fields optional."""

    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)


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
    # Meeting code + shareable link. ``code`` is the display form
    # ``xxx-xxxx-xxx``; the link is ``<app base>/m/<code>``. Both None for
    # meetings that predate codes (and Recall meetings).
    code: str | None = None
    link: str | None = None
    # The hidden ``type="meeting"`` chat room the meeting runs in.
    room_group_id: str | None = None
    access: Literal["ask", "open"] = "ask"
    host_user_id: str | None = None
    description: str | None = None

    # ── Force UTC serialization ───────────────────────────────────────
    # Backend stores all datetimes as naive (timezone-unaware) UTC values.
    # Pydantic's default JSON serialisation omits the timezone suffix,
    # making JS `new Date()` interpret them as LOCAL time instead of UTC.
    # Appending 'Z' ensures every consumer correctly treats them as UTC.
    @field_serializer(
        "scheduled_start",
        "scheduled_end",
        "actual_start",
        "actual_end",
        "created_at",
        "bot_status_at",
    )
    def _serialize_dt(v: datetime | None) -> str | None:
        if v is None:
            return None
        return v.isoformat() + "Z"


class MeetingLookupResponse(BaseModel):
    """GET /meetings/by-code/{code} — public, so only what the join page shows.

    ``status``: ``not_started`` (nobody in the call yet), ``live`` (someone
    other than the call-bot is in it) or ``ended`` (ended, cancelled or the
    link has expired). No ids, emails, members or room names — ever.
    """

    code: str
    title: str | None
    scheduled_start: datetime | None
    host_name: str | None
    access: Literal["ask", "open"]
    status: Literal["not_started", "live", "ended"]

    @field_serializer("scheduled_start")
    def _serialize_start(v: datetime | None) -> str | None:
        if v is None:
            return None
        if v.tzinfo is not None:
            v = v.astimezone(UTC).replace(tzinfo=None)
        return v.isoformat() + "Z"


class JoinMeetingByCodeResponse(BaseModel):
    """POST /meetings/by-code/{code}/join — feed these to the call token flow."""

    room_group_id: str
    room_name: str


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

    @field_serializer("fetched_at")
    def _serialize_fetched_at(v: datetime | None) -> str | None:
        if v is None:
            return None
        return v.isoformat() + "Z"


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

    @field_serializer("last_validated_at")
    def _serialize_validated_at(v: datetime | None) -> str | None:
        if v is None:
            return None
        return v.isoformat() + "Z"


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


# ---------------------------------------------------------------------------
# Lobby — guests ask to join, people in the call admit or deny
# ---------------------------------------------------------------------------

KnockStatus = Literal["waiting", "admitted", "denied", "expired", "cancelled"]


class KnockRequest(BaseModel):
    """POST /meetings/by-code/{code}/knock — public."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] | None = None


class KnockCreatedResponse(BaseModel):
    """The guest keeps ``secret``; it is shown once and needed for every poll."""

    knock_id: str
    secret: str
    status: KnockStatus


class KnockStatusResponse(BaseModel):
    """Guest poll / cancel. The four call fields are set only when the guest may
    connect now (admitted, meeting open, someone in the call); None otherwise."""

    status: KnockStatus
    token: str | None = None
    room_name: str | None = None
    identity: str | None = None
    livekit_url: str | None = None


class KnockSummaryResponse(BaseModel):
    """One waiting guest, for people in the meeting."""

    knock_id: str
    name: str
    email: str | None
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_created(v: datetime) -> str:
        if v.tzinfo is not None:
            v = v.astimezone(UTC).replace(tzinfo=None)
        return v.isoformat() + "Z"


class KnockDecisionResponse(BaseModel):
    knock_id: str
    status: KnockStatus


class UpdateMeetingRequest(BaseModel):
    """PATCH /meetings/{id} — host only. Unknown fields are a 422, not a no-op."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    access: Literal["ask", "open"] | None = None
