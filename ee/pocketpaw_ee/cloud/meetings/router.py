# Meetings — FastAPI router.
# Created: 2026-05-19. Mounted at /api/v1/meetings via mount_cloud().
# See docs/plans/2026-05-19-meetings-integration-design.md.
#
# Routes:
#   GET    /meetings                          — list workspace meetings
#   POST   /meetings                          — create a meeting
#   GET    /meetings/search/                  — cross-provider search
#   GET    /meetings/{meeting_id}             — get one meeting
#   DELETE /meetings/{meeting_id}             — cancel a meeting
#   GET    /meetings/{meeting_id}/transcript  — transcript metadata
#   POST   /meetings/{meeting_id}/bot         — dispatch a Recall.ai bot
#   GET    /meetings/{meeting_id}/bot         — bot lifecycle status
#   DELETE /meetings/{meeting_id}/bot         — stop the bot
#   GET    /meetings/credentials              — provider credential status
#   POST   /meetings/credentials/zoom         — store + validate Zoom creds
#   POST   /meetings/credentials/google_meet  — store Meet OAuth app creds
#   GET    /meetings/credentials/google_meet/auth-url — Meet consent URL
#   POST   /meetings/credentials/google_meet/callback — finish Meet OAuth
#   DELETE /meetings/credentials/{provider}   — disconnect a provider
#   GET    /meetings/settings                 — transcription provider + model
#   PUT    /meetings/settings                 — set transcription provider
#
# Provider credentials (Zoom S2S + Google Meet OAuth) — one deployment-
# global account per provider, configured via the /meetings/credentials/*
# routes (admin-gated, connector.manage) with secret values encrypted at
# rest. The ZOOM_* / GOOGLE_MEET_* environment variables remain a
# fallback. See meetings/credentials.py + service._build_adapter_default.

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import (
    CompleteGoogleMeetOAuthRequest,
    CreateMeetingRequest,
    CredentialsResponse,
    DisconnectResponse,
    GoogleMeetAuthUrlResponse,
    GoogleMeetRedirectUriResponse,
    ListMeetingsRequest,
    MeetingDetailResponse,
    MeetingResponse,
    MeetingsSettingsResponse,
    StoreGoogleMeetCredentialsRequest,
    StoreZoomCredentialsRequest,
    TranscriptResponse,
    UpdateMeetingsSettingsRequest,
)
from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
from pocketpaw_ee.cloud.meetings.providers.recall import credentials as credentials_service
from pocketpaw_ee.cloud.meetings.providers.recall import settings as meetings_settings
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)

router = APIRouter(
    prefix="/meetings",
    tags=["Meetings"],
    dependencies=[Depends(require_license)],
)


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


@router.get("", response_model=list[MeetingResponse])
async def list_meetings(
    workspace_id: str = Depends(current_workspace_id),
    body: ListMeetingsRequest = Depends(),
) -> list[MeetingResponse]:
    """List meetings — server-validated query params via ListMeetingsRequest."""
    return await meetings_service.list_meetings(workspace_id, body)


@router.post("", response_model=MeetingResponse)
async def create_meeting(
    body: CreateMeetingRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> MeetingResponse:
    """Create a meeting via the configured provider adapter."""
    return await meetings_service.create_meeting(workspace_id, user_id, body)


# ---------------------------------------------------------------------------
# Provider credentials — the Settings → Meetings connector page. One
# deployment-global account per provider; admin-gated (connector.manage);
# secret values encrypted at rest. Declared before /{meeting_id} so the
# literal /credentials segment isn't captured as a meeting id.
# ---------------------------------------------------------------------------

_require_admin = Depends(require_action_any_workspace("connector.manage"))


@router.get("/credentials", response_model=list[CredentialsResponse], dependencies=[_require_admin])
async def list_credentials() -> list[CredentialsResponse]:
    """Credential status for every configured meeting provider."""
    return await credentials_service.list_credentials()


@router.get(
    "/credentials/google_meet/redirect-uri",
    response_model=GoogleMeetRedirectUriResponse,
    dependencies=[_require_admin],
)
async def google_meet_redirect_uri() -> GoogleMeetRedirectUriResponse:
    """The redirect URI to register on the Google OAuth client."""
    return credentials_service.get_google_meet_redirect_uri()


@router.get(
    "/credentials/google_meet/auth-url",
    response_model=GoogleMeetAuthUrlResponse,
    dependencies=[_require_admin],
)
async def google_meet_auth_url() -> GoogleMeetAuthUrlResponse:
    """Build the Google consent URL (Meet app credentials must be stored first)."""
    return await credentials_service.get_google_meet_auth_url()


@router.post(
    "/credentials/google_meet/callback",
    response_model=CredentialsResponse,
    dependencies=[_require_admin],
)
async def google_meet_callback(body: CompleteGoogleMeetOAuthRequest) -> CredentialsResponse:
    """Complete Google Meet OAuth — exchange the consent code for a refresh token."""
    return await credentials_service.complete_google_meet_oauth(body)


@router.post("/credentials/zoom", response_model=CredentialsResponse, dependencies=[_require_admin])
async def store_zoom_credentials(body: StoreZoomCredentialsRequest) -> CredentialsResponse:
    """Store + validate Zoom Server-to-Server OAuth credentials."""
    return await credentials_service.store_zoom(body)


@router.post(
    "/credentials/google_meet", response_model=CredentialsResponse, dependencies=[_require_admin]
)
async def store_google_meet_credentials(
    body: StoreGoogleMeetCredentialsRequest,
) -> CredentialsResponse:
    """Store Google Meet OAuth app credentials (consent completed separately)."""
    return await credentials_service.store_google_meet(body)


@router.get(
    "/credentials/{provider}", response_model=CredentialsResponse, dependencies=[_require_admin]
)
async def get_credentials(provider: str) -> CredentialsResponse:
    """One provider's credential status."""
    return await credentials_service.get_credentials(provider)


@router.delete(
    "/credentials/{provider}", response_model=DisconnectResponse, dependencies=[_require_admin]
)
async def disconnect_provider(provider: str) -> DisconnectResponse:
    """Remove a provider's stored credentials."""
    return await credentials_service.disconnect(provider)


# ---------------------------------------------------------------------------
# Transcription settings — realtime vs async + provider / model. Admin-gated.
# Declared before /{meeting_id} so the literal /settings segment isn't
# captured as a meeting id.
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=MeetingsSettingsResponse, dependencies=[_require_admin])
async def get_meetings_settings() -> MeetingsSettingsResponse:
    """The deployment's transcription provider + model + derived mode."""
    return await meetings_settings.get_settings()


@router.put("/settings", response_model=MeetingsSettingsResponse, dependencies=[_require_admin])
async def update_meetings_settings(
    body: UpdateMeetingsSettingsRequest,
) -> MeetingsSettingsResponse:
    """Set the transcription provider + model (realtime or async)."""
    return await meetings_settings.update_settings(body)


@router.get("/{meeting_id}", response_model=MeetingDetailResponse)
async def get_meeting(
    meeting_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> MeetingDetailResponse:
    """One meeting's detail. 404 if not in this workspace."""
    return await meetings_service.get_meeting(workspace_id, meeting_id)


@router.delete("/{meeting_id}", response_model=MeetingResponse)
async def cancel_meeting(
    meeting_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> MeetingResponse:
    """Cancel a meeting. Only the creator (or workspace admin) can cancel."""
    return await meetings_service.cancel_meeting(workspace_id, meeting_id, user_id=user_id)


@router.get("/{meeting_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(
    meeting_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> TranscriptResponse:
    """Transcript metadata for a meeting. 404 if no transcript row exists."""
    return await meetings_service.get_transcript(workspace_id, meeting_id)


# ---------------------------------------------------------------------------
# Recall.ai bot integration — dispatch / status / stop. The captured
# transcript is pushed back via the Svix webhook (meetings/webhooks.py) and
# is also fetchable on demand through ``GET /meetings/{id}/transcript``.
# ---------------------------------------------------------------------------


class RequestBotResponseDTO(BaseModel):
    """Returned by POST /meetings/{id}/bot — Recall.ai bot id + status."""

    bot_id: str
    meeting_id: str
    status: str


@router.post(
    "/{meeting_id}/bot",
    response_model=RequestBotResponseDTO,
    dependencies=[Depends(require_action_any_workspace("connector.execute"))],
)
async def request_bot(
    meeting_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> RequestBotResponseDTO:
    """Dispatch a Recall.ai bot to this meeting to record + transcribe it.

    Returns the bot identifier for tracking; the transcript becomes
    available via ``GET /meetings/{id}/transcript`` once Recall.ai finishes.
    """
    payload = await recall_client.request_bot_for_meeting(workspace_id, meeting_id)
    return RequestBotResponseDTO(
        bot_id=payload.get("bot_id", ""),
        meeting_id=payload.get("meeting_id", meeting_id),
        status=payload.get("status", "queued"),
    )


@router.delete(
    "/{meeting_id}/bot",
    dependencies=[Depends(require_action_any_workspace("connector.execute"))],
)
async def stop_bot(
    meeting_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Stop an active Recall.ai bot for this meeting. Idempotent."""
    return await recall_client.stop_bot(workspace_id, meeting_id)


class BotStatusResponseDTO(BaseModel):
    """Returned by GET /meetings/{id}/bot — the bot's live lifecycle status."""

    meeting_id: str
    has_bot: bool
    bot_id: str | None = None
    status: str | None = None
    status_detail: str | None = None
    status_at: datetime | None = None
    summary: str


@router.get("/{meeting_id}/bot", response_model=BotStatusResponseDTO)
async def get_bot(
    meeting_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> BotStatusResponseDTO:
    """Current Recall.ai bot status for this meeting.

    Live-checked against Recall on each call; the result also refreshes
    the cached ``bot_status`` on the meeting row. Use this for a 'where is
    the bot' poll from the desktop client.
    """
    status = await meetings_service.get_bot_status(workspace_id, meeting_id)
    return BotStatusResponseDTO(**status)


# ---------------------------------------------------------------------------
# Cross-provider aggregation — backs the meetings meta-connector
# ---------------------------------------------------------------------------


@router.get("/search/", response_model=list[MeetingResponse])
async def search_meetings(
    query: str,
    workspace_id: str = Depends(current_workspace_id),
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
) -> list[MeetingResponse]:
    """Cross-provider meeting search by title / organizer / participants.

    Trailing slash is intentional to avoid clashing with ``/{meeting_id}``.
    """

    def _parse(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None

    return await meetings_service.search_meetings(
        workspace_id,
        query=query,
        since=_parse(since),
        until=_parse(until),
        limit=limit,
    )
