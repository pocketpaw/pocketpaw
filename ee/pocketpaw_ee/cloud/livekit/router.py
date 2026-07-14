"""FastAPI REST router for LiveKit call management.

Endpoints:
- ``POST /api/v1/livekit/rooms`` — create/get a call room for a group
- ``POST /api/v1/livekit/token`` — generate participant access token
- ``DELETE /api/v1/livekit/rooms/{group_id}`` — end a call
- ``GET /api/v1/livekit/rooms/{group_id}`` — get room status
- ``POST /api/v1/livekit/rooms/{group_id}/recording/start`` — start recording (owner only)
- ``POST /api/v1/livekit/rooms/{group_id}/recording/stop`` — stop recording (owner only)
- ``GET /api/v1/livekit/rooms/{group_id}/recording`` — get recording status
- ``POST /api/v1/livekit/rooms/{group_id}/invite`` — create meeting invite link
- ``GET /api/v1/livekit/invites/{token}`` — validate invite (public, no auth)
- ``POST /api/v1/livekit/invites/{token}/join`` — join as guest (public, no auth)
- ``GET /api/v1/livekit/rooms/{group_id}/invites`` — list active invites
- ``DELETE /api/v1/livekit/rooms/{group_id}/invites/{invite_id}`` — revoke invite

All routes require an active enterprise license, user authentication,
and group membership, EXCEPT the public invite validate/join endpoints
which allow external guests without a Pocketpaw account. Recording endpoints
additionally require workspace ownership."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.chat.group_service import (
    _get_group_domain_or_404,
    _require_domain_group_member,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.livekit import service as livekit_service
from pocketpaw_ee.cloud.realtime.emit import emit
from pocketpaw_ee.cloud.realtime.events import (
    CallEnded,
    CallParticipantJoined,
    CallParticipantLeft,
    CallStarted,
)
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
    is_new: bool = False


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


class StartRecordingResponse(BaseModel):
    egress_id: str
    room_name: str
    group_id: str
    output_path: str
    status: int = 0
    started_at: int = 0


class StopRecordingResponse(BaseModel):
    egress_id: str
    room_name: str
    group_id: str
    status: int = 0
    output_files: list[dict] = []
    ended_at: int = 0


class RecordingInfoResponse(BaseModel):
    egress_id: str | None = None
    room_name: str = ""
    group_id: str
    status: str = "inactive"
    is_active: bool = False
    started_at: int = 0
    ended_at: int = 0


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
# Workspace owner guard for recording
# ---------------------------------------------------------------------------


async def _require_workspace_owner(
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    """Ensure the current user is the workspace owner.

    Only workspace owners can start/stop call recordings.
    """
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc

    doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
    if doc is None or doc.deleted_at is not None:
        raise Forbidden("workspace.not_found", "Workspace not found")
    if doc.owner != str(user.id):
        raise Forbidden(
            "workspace.not_owner",
            "Only the workspace owner can manage recordings",
        )


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

    result = await livekit_service.create_room(body.group_id, workspace_id, str(user.id))

    # Only emit call.started when the room was actually created (not when
    # someone joins an existing room). The is_new flag is set atomically
    # inside create_room to avoid race conditions.
    if result.get("is_new"):
        try:
            await emit(
                CallStarted(
                    data={
                        "group_id": body.group_id,
                        "room_name": result["room_name"],
                        "url": result["url"],
                        "caller_id": str(user.id),
                        "caller_name": getattr(user, "full_name", None) or str(user.id),
                    }
                )
            )
        except Exception:
            logger.warning("Failed to emit CallStarted event for group %s", body.group_id)

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

    # Notify group members that someone joined the call.
    if gid:
        try:
            await emit(
                CallParticipantJoined(
                    data={
                        "group_id": gid,
                        "room_name": body.room_name,
                        "identity": body.identity,
                        "name": display_name,
                    }
                )
            )
        except Exception:
            pass

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


@router.post("/rooms/{group_id}/leave")
async def leave_call(
    group_id: str,
    user=Depends(current_user),
):
    """Notify that a participant left a call without ending it."""
    gid = group_id
    try:
        await emit(
            CallParticipantLeft(
                data={
                    "group_id": gid,
                    "room_name": livekit_service.room_name_for_group(gid),
                    "identity": str(user.id),
                    "name": user.full_name or str(user.id),
                }
            )
        )
    except Exception:
        pass
    return {"ok": True}


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

    result = await livekit_service.end_room(group_id, workspace_id)

    # Emit realtime event so group members know the call ended
    try:
        await emit(
            CallEnded(
                data={
                    "group_id": group_id,
                    "room_name": result["room_name"],
                }
            )
        )
    except Exception:
        logger.warning("Failed to emit CallEnded event for group %s", group_id)

    return EndCallResponse(**result)


# ---------------------------------------------------------------------------
# Recording endpoints
# ---------------------------------------------------------------------------


@router.post("/rooms/{group_id}/recording/start", response_model=StartRecordingResponse)
async def start_recording(
    group_id: str,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Start recording the call for a group.

    Only the workspace owner can start recordings. The recording is saved
    as an MP4 composite video to the workspace's S3 bucket.
    """
    await require_license()

    # Verify the caller is a member of the target group.
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

    # Only workspace owner can record
    await _require_workspace_owner(user=user, workspace_id=workspace_id)

    try:
        result = await livekit_service.start_room_recording(group_id)
        return StartRecordingResponse(**result)
    except RuntimeError as exc:
        raise Forbidden("recording.already_active", str(exc))


@router.post("/rooms/{group_id}/recording/stop", response_model=StopRecordingResponse)
async def stop_recording(
    group_id: str,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Stop an active call recording.

    Only the workspace owner can stop recordings. The final MP4 will be
    saved to S3 and linked in the /files page.
    """
    await require_license()

    # Verify the caller is a member of the target group.
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

    # Only workspace owner can stop recording
    await _require_workspace_owner(user=user, workspace_id=workspace_id)

    try:
        result = await livekit_service.stop_room_recording(group_id)
    except RuntimeError as exc:
        raise Forbidden("recording.not_active", str(exc))

    # Create a file record so the recording appears in the /files page
    try:
        room_name = livekit_service.room_name_for_group(group_id)
        output_path = result.get("output_files", [{}])[0].get("filename", "")
        if not output_path:
            output_path = livekit_service._recording_output_path(group_id)

        from datetime import UTC, datetime

        from pocketpaw.uploads.file_store import FileRecord
        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        file_record = FileRecord(
            id=result.get("egress_id", group_id),
            storage_key=output_path,
            filename=f"call-recording-{room_name}.mp4",
            mime="video/mp4",
            size=0,  # Size unknown until file is fully written by LiveKit
            owner_id=str(user.id),
            chat_id=group_id,
            created=datetime.now(UTC),
        )
        store = MongoFileStore()
        await store.save_scoped(
            record=file_record,
            workspace=workspace_id,
            folder_path="/recordings",
        )
        logger.info(
            "Created file record for recording %s in workspace %s",
            file_record.id,
            workspace_id,
        )
    except Exception as exc:
        logger.warning("Failed to create file record for recording: %s", exc)

    return StopRecordingResponse(**result)


@router.get("/rooms/{group_id}/recording", response_model=RecordingInfoResponse)
async def get_recording_status(
    group_id: str,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Get the status of a call recording.

    Returns whether a recording is active and its current state.
    """
    await require_license()

    # Verify the caller is a member of the target group.
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

    info = await livekit_service.get_recording_info(group_id)

    # Check if it's really the owner (for UI purposes we show status to
    # all members, but only owner can start/stop)
    is_owner = False
    try:
        await _require_workspace_owner(user=user, workspace_id=workspace_id)
        is_owner = True
    except Forbidden:
        pass

    if info is None:
        return RecordingInfoResponse(
            group_id=group_id,
            status="inactive",
            is_active=is_owner and False,
        )

    # Map protobuf status int to readable string
    # EgressStatus values:
    #   EGRESS_STARTING = 0
    #   EGRESS_ACTIVE = 1
    #   EGRESS_ENDING = 2
    #   EGRESS_COMPLETE = 3
    #   EGRESS_FAILED = 4
    #   EGRESS_ABORTED = 5
    status_map = {
        0: "starting",
        1: "active",
        2: "ending",
        3: "complete",
        4: "failed",
        5: "aborted",
    }
    status_code = info.get("status", 0)
    status_str = status_map.get(status_code, "unknown")
    is_active = status_code in (0, 1, 2)  # starting, active, ending

    return RecordingInfoResponse(
        egress_id=info.get("egress_id"),
        room_name=info.get("room_name", ""),
        group_id=group_id,
        status=status_str,
        is_active=is_active,
        started_at=info.get("started_at", 0),
        ended_at=info.get("ended_at", 0),
    )


# ---------------------------------------------------------------------------
# Meeting invite endpoints
# ---------------------------------------------------------------------------


class CreateInviteRequest(BaseModel):
    display_name: str = ""
    max_uses: int = Field(default=0, ge=0, description="0 = unlimited")
    ttl_hours: int = Field(default=24, ge=1, le=72)


class CreateInviteResponse(BaseModel):
    invite_id: str
    invite_token: str
    group_id: str
    room_name: str
    created_by: str
    expires_at: str
    max_uses: int = 0


class ValidateInviteResponse(BaseModel):
    valid: bool
    room_name: str
    group_id: str
    workspace_id: str
    display_name: str
    is_call_active: bool = False
    participant_count: int = 0
    expires_at: str
    max_uses: int = 0
    use_count: int = 0


class JoinInviteRequest(BaseModel):
    display_name: str = Field(
        ..., min_length=1, max_length=80, description="Display name shown to other participants"
    )


class JoinInviteResponse(BaseModel):
    token: str
    url: str
    room_name: str
    identity: str
    display_name: str
    group_id: str


class ListInvitesResponse(BaseModel):
    invite_id: str
    group_id: str
    room_name: str
    display_name: str
    max_uses: int
    use_count: int
    guest_count: int
    expires_at: str
    created_at: str
    created_by: str


class RevokeInviteResponse(BaseModel):
    invite_id: str
    group_id: str
    revoked: bool = True


@router.post("/rooms/{group_id}/invite", response_model=CreateInviteResponse)
async def create_invite(
    group_id: str,
    body: CreateInviteRequest = CreateInviteRequest(),
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Create a shareable invite link for the active call in this group.

    Returns an ``invite_token`` that can be shared as a URL. External
    guests can use it to join the call without a Pocketpaw account.
    """
    await require_license()

    # Verify the caller is a member of the target group.
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

    # Derive the room name for this group.
    room_name = livekit_service.room_name_for_group(group_id)

    from pocketpaw_ee.cloud.livekit import invites as invite_service

    result = await invite_service.create_meeting_invite(
        workspace_id=workspace_id,
        group_id=group_id,
        room_name=room_name,
        created_by=str(user.id),
        display_name=body.display_name,
        max_uses=body.max_uses,
        ttl_hours=body.ttl_hours,
    )
    return CreateInviteResponse(**result)


@router.get("/rooms/{group_id}/invites", response_model=list[ListInvitesResponse])
async def list_invites(
    group_id: str,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """List all active meeting invites for a group.

    Only group members can see the invite list.
    """
    await require_license()

    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

    from pocketpaw_ee.cloud.livekit import invites as invite_service

    items = await invite_service.list_meeting_invites(group_id)
    return [ListInvitesResponse(**it) for it in items]


@router.get("/invites/{token}", response_model=ValidateInviteResponse)
async def validate_invite(token: str):
    """Validate a meeting invite token (public — no auth required).

    Returns room metadata so the guest join page can show whether the
    call is still active before the guest enters a display name.
    """
    from pocketpaw_ee.cloud.livekit import invites as invite_service

    result = await invite_service.validate_meeting_invite(token)
    return ValidateInviteResponse(**result)


@router.post("/invites/{token}/join", response_model=JoinInviteResponse)
async def join_via_invite(token: str, body: JoinInviteRequest):
    """Join a call as a guest via an invite token (public — no auth required).

    Returns a LiveKit access token so the guest can connect immediately.
    The guest identity is ``guest-{random}`` and the provided
    ``display_name`` is shown to other participants.
    """
    from pocketpaw_ee.cloud.livekit import invites as invite_service

    result = await invite_service.accept_meeting_invite(token, body.display_name)
    return JoinInviteResponse(**result)


@router.delete(
    "/rooms/{group_id}/invites/{invite_id}",
    response_model=RevokeInviteResponse,
)
async def revoke_invite(
    group_id: str,
    invite_id: str,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Revoke a meeting invite so it can no longer be used.

    Only group members can revoke invites.
    """
    await require_license()

    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

    from pocketpaw_ee.cloud.livekit import invites as invite_service

    result = await invite_service.revoke_meeting_invite(invite_id, str(user.id))
    return RevokeInviteResponse(**result)
