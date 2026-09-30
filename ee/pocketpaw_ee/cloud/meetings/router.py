# Meetings — FastAPI router.
# Created: 2026-05-19. Mounted at /api/v1/meetings via mount_cloud().
# See docs/plans/2026-05-19-meetings-integration-design.md.
# 2026-10-01 (feat/meetings-instant, MC-1): POST /meetings/instant.
# 2026-10-01 (feat/meetings-by-code, MC-2): POST /meetings with source=livekit
# and no group_id creates a meeting for later; GET /meetings/by-code/{code}
# (public, per-IP rate limit) and POST /meetings/by-code/{code}/join.
# 2026-10-01 (feat/meetings-lobby, MC-3): the lobby — guest knock / status /
# cancel (public, rate-limited, per-knock secret in the X-Knock-Secret header)
# and member list / admit / deny;
# PATCH /meetings/{id} (host: access, title, description).
#
# Routes:
#   GET    /meetings                          — list workspace meetings
#   POST   /meetings                          — create a meeting
#   POST   /meetings/instant                  — start a meeting now (code + link)
#   GET    /meetings/by-code/{code}           — PUBLIC join-page lookup (6 fields)
#   POST   /meetings/by-code/{code}/join      — member joins by code, call starts
#   POST   /meetings/by-code/{code}/knock     — PUBLIC guest asks to join
#   GET    /meetings/by-code/{code}/knocks/{knock_id} — PUBLIC guest poll (secret)
#   DELETE /meetings/by-code/{code}/knocks/{knock_id} — PUBLIC guest cancels (secret)
#   GET    /meetings/search/                  — cross-provider search
#   GET    /meetings/{meeting_id}             — get one meeting
#   PATCH  /meetings/{meeting_id}             — host edits access/title/description
#   DELETE /meetings/{meeting_id}             — cancel a meeting
#   GET    /meetings/{meeting_id}/knocks      — guests waiting (room members)
#   POST   /meetings/{meeting_id}/knocks/{knock_id}/admit|deny — someone in the call
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

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, field_serializer

from pocketpaw_ee.cloud._core.rate_limit import (
    rate_limit_meeting_knock,
    rate_limit_meeting_knock_poll,
    rate_limit_meeting_lookup,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.meetings import lobby_service
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import (
    CompleteGoogleMeetOAuthRequest,
    CreateMeetingRequest,
    CredentialsResponse,
    DisconnectResponse,
    GoogleMeetAuthUrlResponse,
    GoogleMeetRedirectUriResponse,
    JoinMeetingByCodeResponse,
    KnockCreatedResponse,
    KnockDecisionResponse,
    KnockRequest,
    KnockStatusResponse,
    KnockSummaryResponse,
    ListMeetingsRequest,
    MeetingDetailResponse,
    MeetingLookupResponse,
    MeetingResponse,
    MeetingsSettingsResponse,
    StartInstantMeetingRequest,
    StoreGoogleMeetCredentialsRequest,
    StoreZoomCredentialsRequest,
    TranscriptResponse,
    UpdateMeetingRequest,
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
    """Create a meeting via the configured provider adapter.

    ``source="livekit"`` with no ``group_id`` creates a meeting for later: a
    hidden meeting room plus a code and link, no call started.
    """
    return await meetings_service.create_meeting(workspace_id, user_id, body)


@router.post("/instant", response_model=MeetingResponse)
async def start_instant_meeting(
    body: StartInstantMeetingRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> MeetingResponse:
    """Start a meeting now: hidden meeting room, meeting code + link, live call.

    402 ``billing.call_limit`` when the plan has no call time left today; then
    nothing is created.
    """
    return await meetings_service.start_instant_meeting(workspace_id, user_id, body)


@router.get(
    "/by-code/{code}",
    response_model=MeetingLookupResponse,
    dependencies=[Depends(rate_limit_meeting_lookup)],
)
async def lookup_meeting_by_code(code: str) -> MeetingLookupResponse:
    """PUBLIC — no sign-in. What the ``/m/<code>`` page shows before joining.

    Accepts the code with or without dashes, any case. 404 for an unknown code;
    429 ``meetings.lookup_rate_limited`` past 30 lookups a minute per IP.
    """
    return await meetings_service.lookup_meeting_by_code(code)


@router.post("/by-code/{code}/join", response_model=JoinMeetingByCodeResponse)
async def join_meeting_by_code(
    code: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> JoinMeetingByCodeResponse:
    """Join a meeting by code as a member of its workspace; starts the call if needed.

    403 ``livekit.room_forbidden`` (another workspace, or a chat-room meeting you
    aren't in), 410 ``meeting.ended``, 402 ``billing.call_limit``, 404 unknown.
    """
    return await meetings_service.join_meeting_by_code(workspace_id, user_id, code)


# ---------------------------------------------------------------------------
# Lobby, guest side — PUBLIC. The knock's secret authorises every later read;
# it is read ONLY from the X-Knock-Secret header (a query string would end up in
# access logs).
# ---------------------------------------------------------------------------


@router.post(
    "/by-code/{code}/knock",
    response_model=KnockCreatedResponse,
    dependencies=[Depends(rate_limit_meeting_knock)],
)
async def knock(code: str, body: KnockRequest) -> KnockCreatedResponse:
    """PUBLIC — a guest asks to join. Keep ``secret``; it is shown only here.

    404 unknown code, 410 ``meeting.ended``, 403 ``meeting.email_not_allowed``,
    422 bad name/email, 429 ``meetings.knock_rate_limited``.
    """
    return await lobby_service.knock(code, body)


@router.get(
    "/by-code/{code}/knocks/{knock_id}",
    response_model=KnockStatusResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(rate_limit_meeting_knock_poll)],
)
async def knock_status(
    code: str,
    knock_id: str,
    x_knock_secret: str | None = Header(default=None),
) -> KnockStatusResponse:
    """PUBLIC — the guest's poll (every 2s). ``{status}``, plus ``token``,
    ``room_name``, ``identity`` and ``livekit_url`` when they can connect now.
    404 for an unknown knock or a wrong/missing secret."""
    return await lobby_service.knock_status(code, knock_id, x_knock_secret)


@router.delete(
    "/by-code/{code}/knocks/{knock_id}",
    response_model=KnockStatusResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(rate_limit_meeting_knock_poll)],
)
async def cancel_knock(
    code: str,
    knock_id: str,
    x_knock_secret: str | None = Header(default=None),
) -> KnockStatusResponse:
    """PUBLIC — the guest stops waiting. 409 ``meeting.knock_decided`` once answered."""
    return await lobby_service.cancel_knock(code, knock_id, x_knock_secret)


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


@router.patch("/{meeting_id}", response_model=MeetingResponse)
async def update_meeting(
    meeting_id: str,
    body: UpdateMeetingRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> MeetingResponse:
    """Host only: ``access`` ("ask" | "open"), ``title``, ``description``.

    403 ``meeting.host_only``; 422 for any other field (rescheduling isn't here).
    """
    return await meetings_service.update_meeting(workspace_id, user_id, meeting_id, body)


@router.get("/{meeting_id}/knocks", response_model=list[KnockSummaryResponse])
async def list_knocks(
    meeting_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[KnockSummaryResponse]:
    """Guests waiting to join, oldest first. Members of the meeting room only
    (403 ``livekit.room_forbidden``)."""
    return await lobby_service.list_knocks(workspace_id, user_id, meeting_id)


@router.post("/{meeting_id}/knocks/{knock_id}/admit", response_model=KnockDecisionResponse)
async def admit_knock(
    meeting_id: str,
    knock_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> KnockDecisionResponse:
    """Let a guest in. The caller must be in the call (403 ``meeting.not_in_call``);
    409 ``meeting.knock_decided`` when already answered, cancelled or expired."""
    return await lobby_service.decide_knock(workspace_id, user_id, meeting_id, knock_id, admit=True)


@router.post("/{meeting_id}/knocks/{knock_id}/deny", response_model=KnockDecisionResponse)
async def deny_knock(
    meeting_id: str,
    knock_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> KnockDecisionResponse:
    """Turn a guest away. Same rules as admit."""
    return await lobby_service.decide_knock(
        workspace_id, user_id, meeting_id, knock_id, admit=False
    )


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

    @field_serializer("status_at")
    def _serialize_status_at(v: datetime | None) -> str | None:
        if v is None:
            return None
        return v.isoformat() + "Z"


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
