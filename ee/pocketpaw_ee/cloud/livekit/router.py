"""FastAPI REST router for LiveKit call management.

Endpoints:
- ``POST /api/v1/livekit/rooms`` — create/get a call room for a group
- ``POST /api/v1/livekit/token`` — generate participant access token
- ``DELETE /api/v1/livekit/rooms/{group_id}`` — end a call
- ``GET /api/v1/livekit/rooms/{group_id}`` — get room status

All routes require an active enterprise license, user authentication,
and group membership.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.chat.group_service import (
    _get_group_domain_or_404,
    _require_domain_group_member,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.livekit import service as livekit_service
from pocketpaw_ee.cloud.shared.deps import current_user, current_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/livekit", tags=["LiveKit"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateRoomRequest(BaseModel):
    group_id: str = Field(..., description="The group ID to create a call for")


class CreateRoomResponse(BaseModel):
    room_name: str
    group_id: str
    url: str
    bot_token: str
    created_at: str


class TokenRequest(BaseModel):
    room_name: str = Field(..., description="LiveKit room name")
    identity: str = Field(..., description="Participant identity (user ID)")
    can_publish: bool = True
    can_subscribe: bool = True
    ttl_seconds: int = 3600


class TokenResponse(BaseModel):
    token: str
    url: str
    room_name: str


class RoomInfoResponse(BaseModel):
    room_name: str
    group_id: str
    participant_count: int = 0
    participants: list[dict] = []
    active: bool


class EndCallResponse(BaseModel):
    room_name: str
    group_id: str
    ended_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _group_id_from_room_name(room_name: str) -> str | None:
    """Extract group ID from a LiveKit room name (``group-call-{id}``).

    Returns None if the room name doesn't match the expected pattern.
    """
    prefix = "group-call-"
    if room_name.startswith(prefix):
        return room_name[len(prefix) :]
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/rooms", response_model=CreateRoomResponse)
async def create_room(
    body: CreateRoomRequest,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Create a LiveKit room for a group call.

    If a room already exists for this group, returns the existing one.
    The response includes a short-lived admin token for the call bot.
    """
    await require_license()

    # Verify the caller is a member of the target group.
    group = await _get_group_domain_or_404(body.group_id)
    _require_domain_group_member(group, str(user.id))

    result = await livekit_service.create_room(body.group_id)
    return CreateRoomResponse(**result)


@router.post("/token", response_model=TokenResponse)
async def generate_token(
    body: TokenRequest,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Generate a participant access token for a LiveKit room.

    The token is valid for ``ttl_seconds`` (default 1 hour).
    Participants need this token to join the call.
    The participant's display name is included so other users see the
    real name instead of the user ID.
    """
    await require_license()

    # Verify the caller is a member of the group that owns this room.
    gid = _group_id_from_room_name(body.room_name)
    if gid:
        group = await _get_group_domain_or_404(gid)
        _require_domain_group_member(group, str(user.id))

    # Use the user's full_name as the LiveKit participant name
    display_name = user.full_name or body.identity

    token = await livekit_service.generate_participant_token(
        room_name=body.room_name,
        identity=body.identity,
        name=display_name,
        can_publish=body.can_publish,
        can_subscribe=body.can_subscribe,
        ttl_seconds=body.ttl_seconds,
    )
    return TokenResponse(
        token=token,
        url=livekit_service.LIVEKIT_URL,
        room_name=body.room_name,
    )


@router.get("/rooms/{group_id}")
async def get_room_info(
    group_id: str,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Get the current state of a room (participants, active status).

    Returns a 404-like null response if the room doesn't exist
    (no active call).
    """
    await require_license()

    # Verify the caller is a member of the target group.
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

    info = await livekit_service.get_room_info(group_id)
    if info is None:
        # Return a "room not active" response
        return RoomInfoResponse(
            room_name=livekit_service.room_name_for_group(group_id),
            group_id=group_id,
            active=False,
            participants=[],
        )
    return RoomInfoResponse(**info)


@router.delete("/rooms/{group_id}")
async def end_call(
    group_id: str,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """End an active call by deleting the LiveKit room.

    All participants will be disconnected. The call bot's meeting notes
    will be posted to the group shortly after.
    """
    await require_license()

    # Verify the caller is a member of the target group.
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

    result = await livekit_service.end_room(group_id)
    return EndCallResponse(**result)
