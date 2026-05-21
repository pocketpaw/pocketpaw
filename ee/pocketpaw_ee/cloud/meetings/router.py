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
#
# Provider credentials (Zoom S2S + Google Meet OAuth) are deployment-wide
# environment variables — single-account model, no per-workspace BYO and
# no /credentials sub-resource. See meetings/service._build_adapter_default.

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.meetings import recall_client
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import (
    CreateMeetingRequest,
    ListMeetingsRequest,
    MeetingDetailResponse,
    MeetingResponse,
    TranscriptResponse,
)
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
    """Cancel a meeting via the provider."""
    return await meetings_service.cancel_meeting(workspace_id, meeting_id)


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
