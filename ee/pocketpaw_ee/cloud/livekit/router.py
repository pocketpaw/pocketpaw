"""FastAPI REST router for LiveKit call management (``/api/v1/livekit``).

Rooms and tokens: ``POST /rooms`` (create or fetch a group's call room),
``POST /token``, ``GET`` / ``DELETE /rooms/{group_id}``,
``POST /rooms/{group_id}/leave``. Recording (workspace owner only):
``POST /rooms/{group_id}/recording/start|stop``, ``GET .../recording``.
Guest invites: ``POST`` / ``GET /rooms/{group_id}/invite(s)``,
``DELETE .../invites/{invite_id}``, and the public ``GET /invites/{token}`` and
``POST /invites/{token}/join``. Ask Paw: ``POST /rooms/{group_id}/ask``,
``.../ask/listen`` (push-to-talk start/end), ``.../ask/stop``.

Every route except the two public invite routes needs a license, an
authenticated user, and the audited ``service.require_call_group`` check (the
group is in the caller's workspace and has them as a member; one 403
``livekit.room_forbidden`` for all failures, so it is no existence oracle).
``/token`` mints only for ``group-call-<id>`` rooms, always with the caller's
user id as the LiveKit identity, and clamps the TTL to an hour; the call-bot's
token never leaves the server. Invite join enforces the invite's email
allow-list. ``POST /rooms`` on a hidden meeting room runs the meeting's own row
(410 ``meeting.ended`` once it is over).

Ask Paw also needs the bot allowed for the workspace (403
``livekit.assistant_unavailable``) and a bot in the call (409
``livekit.assistant_offline``); it takes one question per call (409
``livekit.assistant_busy``), only the asker may end their push-to-talk (409
``livekit.assistant_ask_mismatch``), each member gets 10 asks a minute (429),
and Redis being down is 503 ``livekit.assistant_offline``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, Forbidden, RateLimited
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    CallEnded,
    CallParticipantJoined,
    CallParticipantLeft,
    CallStarted,
)
from pocketpaw_ee.cloud.chat.group_service import (
    _get_group_domain_or_404,
    _require_domain_group_member,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.livekit import assistant
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
    # No bot_token: the call-bot's 24h token stays on the server, which uses
    # it to spawn the bot. Handing it to members let them join as call-bot.
    room_name: str
    group_id: str
    url: str
    created_at: str
    is_new: bool = False


# Longest token /token will mint. The frontend asks for 3600 and LiveKit keeps
# a connected participant in the room past the token's expiry, so an hour only
# bounds how long a leaked token can be used to connect.
MAX_TOKEN_TTL_SECONDS = 3600


class TokenRequest(BaseModel):
    room_name: str = Field(..., description="LiveKit room name")
    # Ignored: the identity is always the authenticated user's id. Kept so
    # existing clients that still send it don't get a 422.
    identity: str = Field(default="", description="Ignored; the caller's user id is used")
    can_publish: bool = True
    can_subscribe: bool = True
    ttl_seconds: int = Field(
        default=MAX_TOKEN_TTL_SECONDS, description="Clamped to 60..3600 seconds"
    )


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
    Members get their own token from ``POST /token``; the call-bot's token is
    never returned.
    """
    await require_license()

    # The group must be in the caller's workspace and have them as a member.
    await livekit_service.require_call_group(body.group_id, str(user.id), workspace_id)

    # A hidden meeting room runs its meeting's own row (no "Instant call" twin;
    # 410 meeting.ended once the meeting is over). Any other room as before.
    from pocketpaw_ee.cloud.meetings import service as meetings_service

    result = await meetings_service.start_meeting_room_call(
        workspace_id, str(user.id), body.group_id
    )
    if result is None:
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

    # Every room name must map to a group the caller belongs to in their
    # workspace. Until 2026-09-30 only ``group-call-*`` names were checked, so
    # any other name minted a token with no check at all. One 403 for "not a
    # call room", "no such group", "other workspace" and "not a member", so the
    # response is not an existence oracle. The call-bot and guests never come
    # here: their tokens are minted in create_room and invite accept.
    gid = _group_id_from_room_name(body.room_name)
    await livekit_service.require_call_group(gid, str(user.id), workspace_id)

    # The LiveKit identity is the authenticated user, never the request body:
    # a body-supplied identity let a member join as ``call-bot``, as a
    # ``guest-*`` or as another member. User ids are ObjectIds, so they can't
    # collide with those reserved identities.
    identity = str(user.id)
    if body.identity and body.identity != identity:
        logger.debug("Ignoring client-supplied LiveKit identity for user %s", identity)
    display_name = user.full_name or identity

    token = await livekit_service.generate_participant_token(
        room_name=body.room_name,
        identity=identity,
        name=display_name,
        can_publish=body.can_publish,
        can_subscribe=body.can_subscribe,
        ttl_seconds=min(max(body.ttl_seconds, 60), MAX_TOKEN_TTL_SECONDS),
    )

    # Notify group members that someone joined the call.
    try:
        await emit(
            CallParticipantJoined(
                data={
                    "group_id": gid,
                    "room_name": body.room_name,
                    "identity": identity,
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

    # The group must be in the caller's workspace and have them as a member.
    await livekit_service.require_call_group(group_id, str(user.id), workspace_id)

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
    await require_license()

    # Verify the caller is a member of the target group. This runs OUTSIDE the
    # try below on purpose: the emit's blanket ``except`` must never swallow a
    # 403/404 raised here. ``CallParticipantLeft`` fans out to the group's
    # members, so an unguarded handler let any authenticated user push a
    # fabricated "X left the call" into a group they don't belong to.
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, str(user.id))

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

    # The group must be in the caller's workspace and have them as a member.
    await livekit_service.require_call_group(group_id, str(user.id), workspace_id)

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
# Ask Paw endpoints
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)


class AskListenRequest(BaseModel):
    phase: Literal["start", "end"]
    ask_id: str | None = Field(default=None, max_length=64)


class AskResponse(BaseModel):
    ask_id: str


async def _assistant_redis(op: Awaitable[_T]) -> _T:
    """Ask Paw's lock, rate limit and bot channel all live in Redis."""
    try:
        return await op
    except CloudError:
        raise
    except Exception as exc:
        raise _offline(exc) from exc


def _offline(exc: Exception) -> CloudError:
    logger.warning("Ask Paw: Redis unavailable: %s", exc)
    return CloudError(503, "livekit.assistant_offline", "Paw can't take questions right now.")


async def _require_ask_access(group_id: str, user: Any, workspace_id: str) -> None:
    await require_license()
    await livekit_service.require_call_group(group_id, str(user.id), workspace_id)
    if not await livekit_service._meeting_notes_allowed(workspace_id):
        raise Forbidden(
            "livekit.assistant_unavailable", "Paw isn't available for calls in this workspace."
        )


async def _claim_ask_slot(group_id: str, user_id: str) -> str:
    if not await _assistant_redis(assistant.bot_in_call(group_id)):
        raise ConflictError("livekit.assistant_offline", "Paw isn't in this call.")
    if not await _assistant_redis(assistant.allow_ask(user_id)):
        raise RateLimited(
            "livekit.assistant_rate_limited", "Too many questions - wait a moment and try again."
        )
    ask_id = assistant.new_ask_id()
    if not await _assistant_redis(assistant.acquire_ask_lock(group_id, ask_id, user_id)):
        raise ConflictError(
            "livekit.assistant_busy", "Paw is already answering a question in this call."
        )
    return ask_id


def _display_name(user: Any) -> str:
    return getattr(user, "full_name", None) or str(user.id)


@router.post("/rooms/{group_id}/ask", response_model=AskResponse, status_code=202)
async def ask_paw(
    group_id: str,
    body: AskRequest,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Ask Paw a typed question; the answer is spoken in the call and posted to chat."""
    await _require_ask_access(group_id, user, workspace_id)
    ask_id = await _claim_ask_slot(group_id, str(user.id))
    assistant.start_ask_task(
        group_id,
        assistant.answer_call_question(
            group_id=group_id,
            workspace_id=workspace_id,
            user_id=str(user.id),
            asked_by_name=_display_name(user),
            question=body.question.strip() or body.question,
            ask_id=ask_id,
        ),
    )
    return AskResponse(ask_id=ask_id)


@router.post("/rooms/{group_id}/ask/listen", response_model=AskResponse, status_code=202)
async def ask_paw_listen(
    group_id: str,
    body: AskListenRequest,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Push-to-talk: ``start`` opens a capture window on the caller's mic, ``end`` answers it.

    The caller's LiveKit identity is their user id (see ``/token``), which is
    what the bot keys the capture window on.
    """
    await _require_ask_access(group_id, user, workspace_id)
    user_id = str(user.id)

    name = _display_name(user)

    if body.phase == "start":
        ask_id = await _claim_ask_slot(group_id, user_id)
        try:
            await assistant.begin_listening(ask_id)
            await assistant.publish_bot_command(
                group_id, {"type": "listen_start", "ask_id": ask_id, "identity": user_id}
            )
        except Exception as exc:
            await assistant.abort_claimed_ask(group_id, ask_id, user_id, name)
            raise _offline(exc) from exc
        await assistant.emit_assistant_state(
            group_id=group_id,
            state="listening",
            ask_id=ask_id,
            asked_by=user_id,
            asked_by_name=name,
        )
        assistant.start_listen_watchdog(group_id, ask_id, user_id, name)
        return AskResponse(ask_id=ask_id)

    mismatch = ConflictError(
        "livekit.assistant_ask_mismatch", "That question is no longer being listened for."
    )
    if not body.ask_id:
        raise mismatch
    holder = await _assistant_redis(assistant.ask_lock_holder(group_id))
    asker = await _assistant_redis(assistant.ask_asker(body.ask_id))
    if holder != body.ask_id or asker != user_id:
        raise mismatch
    if not await _assistant_redis(assistant.end_listening(body.ask_id)):
        raise mismatch
    try:
        await assistant.publish_bot_command(
            group_id, {"type": "listen_end", "ask_id": body.ask_id, "identity": user_id}
        )
    except Exception as exc:
        await assistant.abort_claimed_ask(group_id, body.ask_id, user_id, name)
        raise _offline(exc) from exc
    assistant.start_ask_task(
        group_id,
        assistant.answer_spoken_question(
            group_id=group_id,
            workspace_id=workspace_id,
            user_id=user_id,
            asked_by_name=name,
            ask_id=body.ask_id,
        ),
    )
    return AskResponse(ask_id=body.ask_id)


@router.post("/rooms/{group_id}/ask/stop")
async def ask_paw_stop(
    group_id: str,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Silence Paw: any member may stop the current answer, not only the asker."""
    await _require_ask_access(group_id, user, workspace_id)
    holder = await _assistant_redis(assistant.stop_ask(group_id))
    await assistant.emit_assistant_state(
        group_id=group_id,
        state="idle",
        ask_id=holder,
        asked_by=str(user.id),
        asked_by_name=_display_name(user),
    )
    return {"ok": True}


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

    # The group must be in the caller's workspace and have them as a member.
    await livekit_service.require_call_group(group_id, str(user.id), workspace_id)

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

    # The group must be in the caller's workspace and have them as a member.
    await livekit_service.require_call_group(group_id, str(user.id), workspace_id)

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

    # The group must be in the caller's workspace and have them as a member.
    await livekit_service.require_call_group(group_id, str(user.id), workspace_id)

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
    requires_email: bool = False
    is_call_active: bool = False
    participant_count: int = 0
    expires_at: str
    max_uses: int = 0
    use_count: int = 0


class JoinInviteRequest(BaseModel):
    display_name: str = Field(
        ..., min_length=1, max_length=80, description="Display name shown to other participants"
    )
    email: str | None = Field(
        default=None,
        max_length=320,
        description="Guest email; required when the invite has an email allow-list",
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

    result = await invite_service.accept_meeting_invite(token, body.display_name, body.email)
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

    result = await invite_service.revoke_meeting_invite(invite_id, str(user.id), group_id)
    return RevokeInviteResponse(**result)
