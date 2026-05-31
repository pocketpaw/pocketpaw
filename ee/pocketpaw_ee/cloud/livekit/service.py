"""LiveKit service — room management, token generation, call lifecycle.

Uses the ``livekit-api`` Python SDK to talk to LiveKit Cloud.
Requires ``LIVEKIT_URL``, ``LIVEKIT_API_KEY``, ``LIVEKIT_API_SECRET``
environment variables.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from livekit.api import AccessToken, LiveKitAPI, VideoGrants
from livekit.protocol.room import (
    CreateRoomRequest,
    DeleteRoomRequest,
    ListParticipantsRequest,
    ListRoomsRequest,
)

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import CallEnded, CallNotesPosted, CallStarted

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Active meeting agents registry
# ---------------------------------------------------------------------------

from pocketpaw_ee.cloud.livekit.types import MeetingAgentProtocol  # noqa: E402

_active_agents: dict[str, MeetingAgentProtocol] = {}

# Collected meeting-notes payloads from agent subprocesses.
# Populated by _collect_agent_notes (background reader on stdout pipe)
# and consumed by _reap_agent_process (which posts them to the group
# chat using the parent process's Beanie connection).
_agent_notes_payloads: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Active recordings registry  (group_id → egress_id)
# ---------------------------------------------------------------------------

_active_recordings: dict[str, str] = {}


def _get_agent(group_id: str) -> MeetingAgentProtocol | None:
    """Get the active meeting agent for a group, if any."""
    return _active_agents.get(group_id)


# ---------------------------------------------------------------------------
# Configuration — read from environment
# ---------------------------------------------------------------------------

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

# System agent used for bot posting
CALL_BOT_USER_ID = "__livekit_call_bot__"


def _ensure_configured() -> None:
    """Raise if LiveKit credentials are missing."""
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise RuntimeError(
            "LiveKit is not configured. Set LIVEKIT_URL, LIVEKIT_API_KEY, "
            "and LIVEKIT_API_SECRET environment variables."
        )


def room_name_for_group(group_id: str) -> str:
    """Build a deterministic LiveKit room name for a group."""
    return f"group-call-{group_id}"


# ---------------------------------------------------------------------------
# Subprocess agent management (avoids running WebRTC/Deepgram in the
# server event loop).
# ---------------------------------------------------------------------------


class _SubprocessAgentRef:
    """Thin wrapper that implements ``MeetingAgentProtocol`` via a subprocess.

    The real ``CallMeetingAgent`` runs in a child process managed by
    ``asyncio.create_subprocess_exec``.  ``start()`` is a no-op (the
    process starts on construction); ``stop()`` sends SIGTERM.
    """

    def __init__(
        self,
        group_id: str,
        room_name: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        self.group_id = group_id
        self.room_name = room_name
        self._process = process

    async def start(self) -> None:
        """Already started by ``_spawn_agent_process``."""
        pass

    async def stop(self) -> None:
        """Send SIGTERM and wait up to 10 s for graceful exit."""
        if self._process.returncode is not None:
            return  # already terminated
        try:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except TimeoutError:
                logger.warning(
                    "Agent subprocess for room %s did not exit in 10s, killing",
                    self.room_name,
                )
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            pass  # already dead
        logger.info(
            "Agent subprocess for room %s exited (code %s)",
            self.room_name,
            self._process.returncode,
        )


async def _spawn_agent_process(
    group_id: str,
    room_name: str,
    bot_token: str,
) -> asyncio.subprocess.Process:
    """Launch ``CallMeetingAgent`` as a managed subprocess.

    Uses ``sys.executable -m ee.cloud.livekit.agent`` so the child
    inherits the same Python environment and dependencies.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pocketpaw_ee.cloud.livekit.agent",
        "--group",
        group_id,
        "--room",
        room_name,
        "--token",
        bot_token,
        "--url",
        LIVEKIT_URL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Forward stderr to the parent logger so agent logs are visible in
    # the server output.  The background task reads the pipe so the
    # subprocess never blocks on a full buffer.
    asyncio.create_task(_forward_agent_stderr(proc, room_name))

    # Collect meeting notes from stdout.
    # The agent writes a JSON payload to stdout when the call ends.
    # We read it here in the parent process so we can post it to the
    # group chat using the parent's Beanie connection (the subprocess
    # is isolated and doesn't have MongoDB initialized).
    asyncio.create_task(_collect_agent_notes(proc, group_id, room_name))

    logger.info(
        "Spawned agent subprocess (PID %d) for room %s",
        proc.pid,
        room_name,
    )
    return proc


async def _forward_agent_stderr(
    proc: asyncio.subprocess.Process,
    room_name: str,
) -> None:
    """Forward an agent subprocess's stderr to the parent logger."""
    label = f"agent[{room_name}]"
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        if text:
            logger.log(logging.WARNING, "[%s] %s", label, text)


async def _collect_agent_notes(
    proc: asyncio.subprocess.Process,
    group_id: str,
    room_name: str,
) -> None:
    """Read the agent's stdout and capture the meeting notes JSON payload.

    The agent writes a single JSON line to stdout in ``_finalize_notes()``
    with ``type: "meeting_notes"``.  We read it here and store it in
    ``_agent_notes_payloads`` so that ``_reap_agent_process`` can post it
    to the group chat using the parent process's Beanie connection.

    Runs as a background ``asyncio.Task``.  The pipe is unbuffered enough
    that the agent's ``print(..., flush=True)`` won't block.
    """
    import json

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
                if isinstance(payload, dict) and payload.get("type") == "meeting_notes":
                    _agent_notes_payloads[group_id] = payload
                    logger.info(
                        "Collected meeting notes payload from agent for room %s",
                        room_name,
                    )
            except json.JSONDecodeError:
                pass  # ignore non-JSON output (shouldn't happen)
    except Exception as exc:
        logger.warning("Error reading agent stdout for %s: %s", room_name, exc)


async def _reap_agent_process(
    group_id: str,
    proc: asyncio.subprocess.Process,
) -> None:
    """Wait for an agent subprocess to finish, then clean up the registry.

    Runs in a background ``asyncio.Task``.  When the agent detects the
    room is empty and exits on its own, this removes it from
    ``_active_agents`` so the registry doesn't leak.

    If the agent exited on its own (natural end — room empty), emit
    ``CallEnded`` so all connected clients refresh their message list
    and see the meeting notes.  When ``end_room()`` kills the agent
    manually it already pops the agent from ``_active_agents``, so
    ``pop`` returns ``None`` and we skip the duplicate emit.
    """
    was_registered = False
    try:
        await proc.wait()
    except asyncio.CancelledError:
        # Server is shutting down — kill the child and let it go.
        if proc.returncode is None:
            proc.kill()
        raise
    finally:
        was_registered = _active_agents.pop(group_id, None) is not None
        logger.info(
            "Reaped agent subprocess for group %s (exit code %s)",
            group_id,
            proc.returncode,
        )

    # ── Post meeting notes (parent process handles the DB write) ──
    # The agent subprocess outputs a JSON payload to stdout with the
    # meeting notes data.  We read it via _collect_agent_notes and
    # post it here where Beanie/MongoDB is already initialized.
    # Yield control first so _collect_agent_notes can drain the pipe.
    await asyncio.sleep(0)
    payload = _agent_notes_payloads.pop(group_id, None)
    if payload is not None:
        try:
            await post_meeting_notes_to_group(
                group_id=group_id,
                transcript=payload.get("transcript", ""),
                summary=payload.get("summary", "Call ended."),
                action_items=payload.get("action_items", []),
                participants=payload.get("participants", []),
                duration_seconds=payload.get("duration_seconds", 0),
            )
            logger.info("Posted meeting notes for group %s from agent payload", group_id)
        except Exception:
            logger.exception(
                "Failed to post meeting notes for group %s from agent payload",
                group_id,
            )

    # Emit CallEnded for natural end — notes are now in the DB,
    # so tell all connected clients to refresh and display them.
    if was_registered:
        try:
            from pocketpaw_ee.cloud._core.realtime.emit import emit as realtime_emit
            from pocketpaw_ee.cloud._core.realtime.events import CallEnded as _CallEnded

            await realtime_emit(
                _CallEnded(
                    data={
                        "group_id": group_id,
                        "room_name": room_name_for_group(group_id),
                    }
                )
            )
            logger.info("Emitted CallEnded for natural room end (group %s)", group_id)
        except Exception:
            logger.debug("Could not emit CallEnded for natural room end (shutdown?)")


# ---------------------------------------------------------------------------
# S3 configuration for LiveKit recording output
# ---------------------------------------------------------------------------


def _get_s3_config() -> dict[str, str] | None:
    """Read S3 config from environment for LiveKit Egress output.

    Returns a dict with keys used by ``livekit.protocol.egress.S3Upload``
    or ``None`` if S3 is not configured (falls back to temp local storage).
    """
    bucket = os.environ.get("S3_PRIVATE_BUCKET") or os.environ.get("S3_BUCKET")
    if not bucket:
        logger.warning("S3 not configured — recordings will use LiveKit's built-in storage")
        return None
    return {
        "bucket": bucket,
        "region": os.environ.get("S3_REGION", ""),
        "endpoint": os.environ.get("S3_ENDPOINT", ""),
        "access_key": os.environ.get("S3_ACCESS_KEY_ID", ""),
        "secret": os.environ.get("S3_SECRET_ACCESS_KEY", ""),
    }


# ---------------------------------------------------------------------------
# Recording management
# ---------------------------------------------------------------------------

RECORDING_DIR = "recordings"


def _recording_output_path(group_id: str) -> str:
    """Build a deterministic S3 key for a recording."""
    from datetime import UTC, datetime

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{RECORDING_DIR}/{group_id}/{timestamp}_{room_name_for_group(group_id)}.mp4"


async def start_room_recording(group_id: str) -> dict[str, Any]:
    """Start a composite room recording via LiveKit Egress, outputting to S3.

    Returns the egress metadata including the egress ID and output path.

    Raises ``RuntimeError`` if a recording is already active for this group.
    """
    _ensure_configured()

    if group_id in _active_recordings:
        raise RuntimeError(f"Recording already active for group {group_id}")

    room_name = room_name_for_group(group_id)

    from livekit.api import LiveKitAPI
    from livekit.protocol.egress import (
        EncodedFileOutput,
        EncodedFileType,
        EncodingOptionsPreset,
        RoomCompositeEgressRequest,
        S3Upload,
    )

    output_path = _recording_output_path(group_id)
    s3_cfg = _get_s3_config()

    file_output = EncodedFileOutput(
        file_type=EncodedFileType.MP4,
        filepath=output_path,
        disable_manifest=False,
    )

    # Configure S3 upload destination
    if s3_cfg:
        file_output.s3.CopyFrom(
            S3Upload(
                bucket=s3_cfg["bucket"],
                region=s3_cfg["region"],
                endpoint=s3_cfg["endpoint"],
                access_key=s3_cfg["access_key"],
                secret=s3_cfg["secret"],
            )
        )
    # If no S3 config, LiveKit uses its built-in storage (if configured)
    # or the recording will fail — log a warning.

    req = RoomCompositeEgressRequest(
        room_name=room_name,
        preset=EncodingOptionsPreset.H264_720P_30,
        audio_only=False,
        file_outputs=[file_output],
    )

    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        try:
            info = await lk.egress.start_room_composite_egress(req)
            egress_id = info.egress_id
            _active_recordings[group_id] = egress_id
            logger.info(
                "Started recording for room %s (egress_id=%s, output=%s)",
                room_name,
                egress_id,
                output_path,
            )
            return {
                "egress_id": egress_id,
                "room_name": room_name,
                "group_id": group_id,
                "output_path": output_path,
                "status": info.status,
                "started_at": info.started_at,
            }
        except Exception as exc:
            logger.error("Failed to start recording for room %s: %s", room_name, exc)
            raise


async def stop_room_recording(group_id: str) -> dict[str, Any]:
    """Stop an active room recording.

    Returns the final egress info including the S3 output path so the
    caller can create a file record in the uploads system.

    Raises ``RuntimeError`` if no recording is active for this group.
    """
    _ensure_configured()

    egress_id = _active_recordings.pop(group_id, None)
    if not egress_id:
        raise RuntimeError(f"No active recording for group {group_id}")

    from livekit.api import LiveKitAPI
    from livekit.protocol.egress import StopEgressRequest

    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        try:
            stop_req = StopEgressRequest(egress_id=egress_id)
            info = await lk.egress.stop_egress(stop_req)
            logger.info(
                "Stopped recording for group %s (egress_id=%s, status=%s)",
                group_id,
                egress_id,
                info.status,
            )

            # Collect output file info
            output_files = []
            for f in info.file_results or []:
                output_files.append(
                    {
                        "filename": f.filename,
                        "size": f.size,
                        "duration": f.duration,
                    }
                )

            return {
                "egress_id": egress_id,
                "room_name": info.room_name or room_name_for_group(group_id),
                "group_id": group_id,
                "status": info.status,
                "output_files": output_files,
                "ended_at": info.ended_at,
            }
        except Exception as exc:
            logger.error("Failed to stop recording for group %s: %s", group_id, exc)
            raise


async def get_recording_info(group_id: str) -> dict[str, Any] | None:
    """Get the current status of a recording for a group.

    Returns ``None`` if no recording was ever started.
    """
    egress_id = _active_recordings.get(group_id)
    if not egress_id:
        return None

    _ensure_configured()

    from livekit.api import LiveKitAPI
    from livekit.protocol.egress import ListEgressRequest

    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        try:
            list_req = ListEgressRequest(room_name=room_name_for_group(group_id))
            resp = await lk.egress.list_egress(list_req)
            for item in resp.items or []:
                if item.egress_id == egress_id:
                    return {
                        "egress_id": item.egress_id,
                        "room_name": item.room_name,
                        "group_id": group_id,
                        "status": item.status,
                        "started_at": item.started_at,
                        "ended_at": item.ended_at,
                    }
            return {
                "egress_id": egress_id,
                "group_id": group_id,
                "status": "unknown",
            }
        except Exception as exc:
            logger.warning("Failed to get recording info for group %s: %s", group_id, exc)
            return {
                "egress_id": egress_id,
                "group_id": group_id,
                "status": "unknown",
            }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_room(group_id: str) -> dict[str, Any]:
    """Create a LiveKit room for a group call.

    Returns room metadata including the room name and the admin token.
    The room is named ``group-call-{group_id}`` for deterministic lookup.
    Automatically starts the meeting notes agent for this room.

    The returned dict includes an ``is_new`` boolean indicating whether
    the room was just created or already existed.
    """
    _ensure_configured()

    room_name = room_name_for_group(group_id)

    is_new = False
    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        # Check if the room already exists FIRST (LiveKit's CreateRoom may be
        # idempotent — succeeding without error for existing rooms — so we
        # cannot rely on catching "already exists" errors).
        list_req = ListRoomsRequest(names=[room_name])
        list_resp = await lk.room.list_rooms(list_req)
        room_exists = len(list_resp.rooms) > 0

        if not room_exists:
            req = CreateRoomRequest(
                name=room_name,
                empty_timeout=5 * 60,
                max_participants=50,
            )
            await lk.room.create_room(req)
            logger.info("Created LiveKit room %s for group %s", room_name, group_id)
            is_new = True
        else:
            logger.info("LiveKit room %s already exists for group %s", room_name, group_id)

    # Generate a subscriber-only token for the call bot (no agent flag
    # so it auto-subscribes to remote tracks for transcription).
    # Use a 24h TTL so the bot never expires mid-call.
    bot_token = await _generate_token(
        room_name,
        "call-bot",
        can_publish=False,
        can_subscribe=True,
        is_admin=False,
        ttl_seconds=86400,
    )

    # Start the meeting notes agent as a managed subprocess so it does not
    # block the server event loop with WebRTC / Deepgram STT processing.
    if group_id not in _active_agents:
        proc = await _spawn_agent_process(
            group_id=group_id,
            room_name=room_name,
            bot_token=bot_token,
        )
        agent_ref = _SubprocessAgentRef(
            group_id=group_id,
            room_name=room_name,
            process=proc,
        )
        _active_agents[group_id] = agent_ref

        # Background task: wait for the subprocess to finish, then clean
        # up the registry so we don't leak agent references.
        asyncio.create_task(_reap_agent_process(group_id, proc))

        logger.info("Started meeting agent subprocess for group %s (room %s)", group_id, room_name)

    await emit(CallStarted(data={"group_id": group_id, "room_name": room_name}))

    return {
        "room_name": room_name,
        "group_id": group_id,
        "url": LIVEKIT_URL,
        "bot_token": bot_token,
        "created_at": datetime.now(UTC).isoformat(),
        "is_new": is_new,
    }


async def generate_participant_token(
    room_name: str,
    identity: str,
    *,
    name: str | None = None,
    can_publish: bool = True,
    can_subscribe: bool = True,
    ttl_seconds: int = 3600,
) -> str:
    """Generate a LiveKit access token for a participant.

    Args:
        room_name: The LiveKit room name.
        identity: Participant identity (usually user ID).
        name: Display name to show to other participants. Falls back to identity.
        can_publish: Whether the participant can publish audio/video.
        can_subscribe: Whether the participant can subscribe to others.
        ttl_seconds: Token time-to-live.

    Returns:
        A JWT token string for connecting to LiveKit.
    """
    _ensure_configured()
    return await _generate_token(
        room_name,
        identity,
        name=name,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        ttl_seconds=ttl_seconds,
    )


async def end_room(group_id: str) -> dict[str, Any]:
    """End an active call by deleting the LiveKit room.

    When the room is deleted, all participants are disconnected and the
    call bot's cleanup logic (posting meeting notes) is triggered.
    """
    _ensure_configured()

    room_name = room_name_for_group(group_id)

    # Stop the meeting agent first (generates and posts notes)
    agent = _active_agents.pop(group_id, None)
    if agent is not None:
        try:
            await agent.stop()
            logger.info("Stopped meeting agent for group %s", group_id)
        except Exception as exc:
            logger.warning("Error stopping meeting agent for group %s: %s", group_id, exc)

    # ── Post meeting notes synchronously ──
    # The agent wrote a JSON payload to stdout before exiting.
    # _collect_agent_notes stored it in _agent_notes_payloads.
    # We post it here (in the parent's Beanie context) so that notes
    # are in the DB BEFORE the room is deleted and CallEnded is
    # emitted by the router layer.
    # Yield control first so _collect_agent_notes can drain the pipe.
    await asyncio.sleep(0)
    payload = _agent_notes_payloads.pop(group_id, None)
    if payload is not None:
        try:
            await post_meeting_notes_to_group(
                group_id=group_id,
                transcript=payload.get("transcript", ""),
                summary=payload.get("summary", "Call ended."),
                action_items=payload.get("action_items", []),
                participants=payload.get("participants", []),
                duration_seconds=payload.get("duration_seconds", 0),
            )
            logger.info("Posted meeting notes for group %s (end_room)", group_id)
        except Exception as exc:
            logger.warning("Failed to post meeting notes for group %s: %s", group_id, exc)

    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        try:
            req = DeleteRoomRequest(room=room_name)
            await lk.room.delete_room(req)
            logger.info("Deleted LiveKit room %s for group %s", room_name, group_id)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                logger.warning("LiveKit room %s not found (already deleted)", room_name)
            else:
                logger.error("Failed to delete LiveKit room: %s", exc)
                raise

    await emit(CallEnded(data={"group_id": group_id, "room_name": room_name}))

    return {
        "room_name": room_name,
        "group_id": group_id,
        "ended_at": datetime.now(UTC).isoformat(),
    }


async def get_room_info(group_id: str) -> dict[str, Any] | None:
    """Get the current state of a LiveKit room.

    Returns None if the room doesn't exist (no active call).
    """
    _ensure_configured()

    room_name = room_name_for_group(group_id)

    async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
        try:
            list_req = ListRoomsRequest(names=[room_name])
            list_resp = await lk.room.list_rooms(list_req)
            if not list_resp.rooms:
                return None

            parts_req = ListParticipantsRequest(room=room_name)
            parts_resp = await lk.room.list_participants(parts_req)
            participants = parts_resp.participants

            return {
                "room_name": room_name,
                "group_id": group_id,
                "participant_count": len(participants),
                "participants": [
                    {
                        "identity": p.identity,
                        "name": p.name or p.identity,
                        "joined_at": _format_joined_at(p.joined_at),
                        "kind": str(p.kind),
                    }
                    for p in participants
                ],
                "active": True,
            }
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg:
                return None
            logger.error("Failed to get room info: %s", exc)
            raise


# ---------------------------------------------------------------------------
# Meeting notes — called by the agent after call ends
# ---------------------------------------------------------------------------


async def post_meeting_notes_to_group(
    group_id: str,
    transcript: str,
    summary: str,
    action_items: list[str],
    participants: list[str],
    duration_seconds: int,
) -> None:
    """Post meeting notes to a group after a call ends.

    Creates a system message directly (bypassing membership check since the
    call-bot is not a group member) and emits a real-time event so all
    group members see it.
    """
    lines = [
        "📋 **Meeting Notes**",
        "",
        f"**Duration:** {_format_duration(duration_seconds)}",
        f"**Participants:** {', '.join(participants) if participants else 'N/A'}",
        "",
        "**Summary:**",
        summary,
    ]

    if action_items:
        lines.append("")
        lines.append("**Action Items:**")
        for i, item in enumerate(action_items, 1):
            lines.append(f"{i}. {item}")

    if transcript:
        lines.append("")
        lines.append("**Transcript:**")
        # Truncate transcript to stay within message limits
        max_transcript_len = 4000
        transcript_preview = transcript[:max_transcript_len]
        if len(transcript) > max_transcript_len:
            transcript_preview += "\n\n*(transcript truncated)*"
        lines.append(f"```\n{transcript_preview}\n```")

    content = "\n".join(lines)

    try:
        # Use direct message creation to bypass membership check

        from pocketpaw_ee.cloud.chat.message_service import _create_group_message_doc
        from pocketpaw_ee.cloud.shared.events import event_bus

        domain_msg = await _create_group_message_doc(
            group_id=group_id,
            sender=CALL_BOT_USER_ID,
            sender_type="user",
            sender_name="Meeting Notes",
            content=content,
        )
        # Emit on internal event bus (group stats, mention notifications)
        await event_bus.emit(
            "message.sent",
            {
                "group_id": group_id,
                "message_id": domain_msg.id,
                "sender_id": CALL_BOT_USER_ID,
                "sender_type": "user",
                "content": content,
                "mentions": [],
            },
        )
        await emit(
            CallNotesPosted(
                data={
                    "group_id": group_id,
                    "message_id": domain_msg.id,
                    "duration_seconds": duration_seconds,
                    "participant_count": len(participants),
                }
            )
        )
        logger.info("Posted meeting notes to group %s (message %s)", group_id, domain_msg.id)
    except Exception as exc:
        logger.error("Failed to post meeting notes to group %s: %s", group_id, exc)
        raise


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _generate_token(
    room_name: str,
    identity: str,
    *,
    name: str | None = None,
    can_publish: bool = True,
    can_subscribe: bool = True,
    is_admin: bool = False,
    ttl_seconds: int = 3600,
) -> str:
    """Internal: generate a LiveKit access token."""
    grants = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_sources=["microphone", "camera", "screen_share"] if can_publish else None,
        can_update_own_metadata=True,
        agent=is_admin or None,
    )

    # Use the provided display name, falling back to the identity (user ID)
    display_name = name or identity

    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_ttl(timedelta(seconds=ttl_seconds))
        .with_grants(grants)
        .with_name(display_name)
    )

    jwt = token.to_jwt()
    return jwt


def _format_duration(seconds: int) -> str:
    """Format seconds into a human-readable duration string."""
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


def _format_joined_at(joined_at: Any) -> str | None:
    """Format a LiveKit participant's joined_at timestamp to ISO string.

    The livekit-api may return joined_at as a protobuf Timestamp (with
    ``ToDatetime()``) or as a plain Unix timestamp (int/float).  Handle both.
    """
    if joined_at is None:
        return None
    if hasattr(joined_at, "ToDatetime"):
        return joined_at.ToDatetime().isoformat()
    if isinstance(joined_at, int | float):
        return datetime.fromtimestamp(joined_at, tz=UTC).isoformat()
    return str(joined_at)
