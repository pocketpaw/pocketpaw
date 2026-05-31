"""FastAPI REST router for LiveKit call management.

Endpoints:
- ``POST /api/v1/livekit/rooms`` — create/get a call room for a group
- ``POST /api/v1/livekit/token`` — generate participant access token
- ``DELETE /api/v1/livekit/rooms/{group_id}`` — end a call
- ``GET /api/v1/livekit/rooms/{group_id}`` — get room status
- ``POST /api/v1/livekit/rooms/{group_id}/recording/start`` — start recording (owner only)
- ``POST /api/v1/livekit/rooms/{group_id}/recording/stop`` — stop recording (owner only)
- ``GET /api/v1/livekit/rooms/{group_id}/recording`` — get recording status

All routes require an active enterprise license, user authentication,
and group membership. Recording endpoints additionally require workspace
ownership."""

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
from pocketpaw_ee.cloud.realtime.events import CallEnded, CallStarted
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

    result = await livekit_service.create_room(body.group_id)

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
