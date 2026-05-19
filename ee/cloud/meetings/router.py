# Meetings — FastAPI router.
# Created: 2026-05-19. Mounted at /api/v1/meetings via mount_cloud().
# See docs/plans/2026-05-19-meetings-integration-design.md.
#
# Routes:
#   GET    /meetings                              — list workspace meetings
#   POST   /meetings                              — create a meeting (stub in 1.3)
#   GET    /meetings/{meeting_id}                 — get one meeting
#   DELETE /meetings/{meeting_id}                 — cancel a meeting (stub in 1.3)
#   GET    /meetings/{meeting_id}/transcript      — transcript metadata
#
# Credentials sub-resource:
#   GET    /meetings/credentials                  — list configured providers
#   GET    /meetings/credentials/{provider}       — one provider's state
#   POST   /meetings/credentials/zoom             — paste Zoom S2S creds
#   POST   /meetings/credentials/google_meet      — paste Google Meet client creds
#   DELETE /meetings/credentials/{provider}       — disconnect provider
#
# RBAC: connector.manage for credentials writes (admin); meeting.use for
# meeting reads/creates (member). Mirrors the connectors router's pattern.

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ee.cloud.license import require_license
from ee.cloud.meetings import bot_coordinator, oauth_flow
from ee.cloud.meetings import credentials as creds_service
from ee.cloud.meetings import service as meetings_service
from ee.cloud.meetings.domain import MeetingProvider
from ee.cloud.meetings.dto import (
    CompleteGoogleMeetOAuthRequest,
    CreateMeetingRequest,
    CredentialsResponse,
    ListMeetingsRequest,
    MeetingDetailResponse,
    MeetingResponse,
    StoreGoogleMeetCredentialsRequest,
    StoreZoomCredentialsRequest,
    TranscriptResponse,
)
from ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)

router = APIRouter(
    prefix="/meetings",
    tags=["Meetings"],
    dependencies=[Depends(require_license)],
)


def _public_base_url(request: Request) -> str:
    """Derive the public base URL from the incoming request.

    Used only to render the Google OAuth ``redirect_uri`` so it matches
    the URL Google calls. Reverse-proxies (Traefik / Coolify) populate
    the Host header; FastAPI's ``request.base_url`` reflects it.
    """
    return str(request.base_url).rstrip("/")


def _snapshot_to_response(snapshot) -> CredentialsResponse:
    return CredentialsResponse(
        provider=snapshot.provider,
        enabled=snapshot.enabled,
        has_credentials=snapshot.has_credentials,
        last_validated_at=snapshot.last_validated_at,
        last_error=snapshot.last_error,
    )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@router.get("/credentials", response_model=list[CredentialsResponse])
async def list_credentials(
    workspace_id: str = Depends(current_workspace_id),
) -> list[CredentialsResponse]:
    """List BYO-creds state for every provider in this workspace."""
    snapshots = await creds_service.list_snapshots(workspace_id)
    return [_snapshot_to_response(s) for s in snapshots]


@router.get("/credentials/{provider}", response_model=CredentialsResponse)
async def get_credentials(
    provider: MeetingProvider,
    workspace_id: str = Depends(current_workspace_id),
) -> CredentialsResponse:
    """One provider's creds state. 404 if never configured."""
    snapshot = await creds_service.get_snapshot(workspace_id, provider)
    if snapshot is None:
        from ee.cloud._core.errors import NotFound

        raise NotFound("meeting_credentials", provider)
    return _snapshot_to_response(snapshot)


@router.post(
    "/credentials/zoom",
    response_model=CredentialsResponse,
    dependencies=[Depends(require_action_any_workspace("connector.manage"))],
)
async def store_zoom_credentials(
    body: StoreZoomCredentialsRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> CredentialsResponse:
    """Persist Zoom S2S OAuth creds; validates via a token exchange."""
    snapshot = await creds_service.store_zoom(workspace_id, body)
    return _snapshot_to_response(snapshot)


@router.post(
    "/credentials/google_meet",
    response_model=CredentialsResponse,
    dependencies=[Depends(require_action_any_workspace("connector.manage"))],
)
async def store_google_meet_credentials(
    body: StoreGoogleMeetCredentialsRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> CredentialsResponse:
    """Persist the OAuth app client creds; consent flow happens next."""
    snapshot = await creds_service.store_google_meet_init(workspace_id, body)
    return _snapshot_to_response(snapshot)


class GoogleMeetAuthUrlResponse(BaseModel):
    """Response for GET /credentials/google_meet/auth-url."""

    auth_url: str
    redirect_uri: str


class GoogleMeetRedirectUriResponse(BaseModel):
    """Response for GET /credentials/google_meet/redirect-uri."""

    redirect_uri: str


def _google_meet_redirect_uri(request: Request) -> str:
    """Derive the canonical Meet OAuth callback URI from this request."""
    return f"{_public_base_url(request)}/api/v1/meetings/credentials/google_meet/callback"


@router.get(
    "/credentials/google_meet/redirect-uri",
    response_model=GoogleMeetRedirectUriResponse,
)
async def get_google_meet_redirect_uri(
    request: Request,
    workspace_id: str = Depends(current_workspace_id),  # noqa: ARG001 — gate on auth
) -> GoogleMeetRedirectUriResponse:
    """Return the redirect URI the admin must register in their Google
    Cloud OAuth client *before* attempting the consent flow.

    Pre-flight surface — does NOT require the admin to have pasted
    creds yet. Without this, the first ``Connect`` click fails with
    Google's ``redirect_uri_mismatch`` error and the only fix is to
    figure out the URI from logs.
    """
    return GoogleMeetRedirectUriResponse(redirect_uri=_google_meet_redirect_uri(request))


@router.get(
    "/credentials/google_meet/auth-url",
    response_model=GoogleMeetAuthUrlResponse,
    dependencies=[Depends(require_action_any_workspace("connector.manage"))],
)
async def get_google_meet_auth_url(
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
) -> GoogleMeetAuthUrlResponse:
    """Build the Google consent URL the user is redirected to."""
    redirect_uri = _google_meet_redirect_uri(request)
    auth_url = await oauth_flow.get_auth_url(workspace_id, redirect_uri=redirect_uri)
    return GoogleMeetAuthUrlResponse(auth_url=auth_url, redirect_uri=redirect_uri)


@router.post(
    "/credentials/google_meet/callback",
    response_model=CredentialsResponse,
    dependencies=[Depends(require_action_any_workspace("connector.manage"))],
)
async def complete_google_meet_oauth(
    body: CompleteGoogleMeetOAuthRequest,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
) -> CredentialsResponse:
    """Exchange ``code`` for tokens, enable the credentials row."""
    redirect_uri = f"{_public_base_url(request)}/api/v1/meetings/credentials/google_meet/callback"
    authorized_ws = await oauth_flow.complete_callback(body, redirect_uri=redirect_uri)
    if authorized_ws != workspace_id:
        from ee.cloud._core.errors import Forbidden

        raise Forbidden(
            "meeting.oauth_workspace_mismatch",
            "OAuth state authorized a different workspace.",
        )
    snapshot = await creds_service.get_snapshot(workspace_id, "google_meet")
    if snapshot is None:
        from ee.cloud._core.errors import NotFound

        raise NotFound("meeting_credentials", "google_meet")
    return _snapshot_to_response(snapshot)


class DisconnectResponse(BaseModel):
    """Trivial 200 envelope so callers don't have to interpret empty body."""

    provider: MeetingProvider
    disconnected: bool = True


@router.delete(
    "/credentials/{provider}",
    response_model=DisconnectResponse,
    dependencies=[Depends(require_action_any_workspace("connector.manage"))],
)
async def disconnect_provider(
    provider: MeetingProvider,
    workspace_id: str = Depends(current_workspace_id),
) -> DisconnectResponse:
    """Disable creds for one provider and delete the on-disk token blob."""
    await creds_service.disconnect(workspace_id, provider)
    return DisconnectResponse(provider=provider)


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

    Phase 1.3: stubbed — returns 422 ``meeting.adapter_not_wired`` until
    Phase 1.5 lands the adapter dispatch.
    """
    return await meetings_service.create_meeting(workspace_id, user_id, body)


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
) -> MeetingResponse:
    """Cancel a meeting via the provider. Stubbed in Phase 1.3."""
    return await meetings_service.cancel_meeting(workspace_id, meeting_id)


@router.get("/{meeting_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(
    meeting_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> TranscriptResponse:
    """Transcript metadata for a meeting. 404 if no transcript row exists."""
    return await meetings_service.get_transcript(workspace_id, meeting_id)


# ---------------------------------------------------------------------------
# Vexa bot integration — request + stop. Transcripts arrive via the
# on-demand polling path in ``meetings_service.fetch_and_store_transcript``;
# Vexa does NOT push to us, so there is no callback endpoint.
# ---------------------------------------------------------------------------


class RequestBotResponseDTO(BaseModel):
    """Returned by POST /meetings/{id}/bot — Vexa bot identifier + status."""

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
    """Ask Vexa to send a bot to this meeting to capture audio + transcript.

    Vexa runs as a separate service stack — see
    docs/plans/2026-05-19-meetings-integration-design.md (Phase B+ update).
    Returns Vexa's bot identifier for tracking; the transcript becomes
    available via ``GET /meetings/{id}/transcript`` once Vexa is done.
    """
    payload = await bot_coordinator.request_bot_for_meeting(workspace_id, meeting_id)
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
    """Stop an active Vexa bot for this meeting. Idempotent."""
    return await bot_coordinator.stop_bot(workspace_id, meeting_id)


# ---------------------------------------------------------------------------
# Cross-provider aggregation (Phase 1.7 — backs the meetings meta-connector)
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
    from datetime import datetime

    def _parse(value: str | None):
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None

    return await meetings_service.search_meetings(
        workspace_id,
        query=query,
        since=_parse(since),
        until=_parse(until),
        limit=limit,
    )
