"""LiveKit MeetingProvider — wraps existing livekit.service for the
unified meetings platform."""

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud.livekit import service as livekit_service
from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest
from pocketpaw_ee.cloud.meetings.providers.base import (
    ProviderCreateResult,
    ProviderStartResult,
    RecordingRef,
)
from pocketpaw_ee.cloud.meetings.providers.livekit import recording as livekit_recording


class LiveKitProvider:
    """Implements MeetingProvider + SupportsRecording for source='livekit'.

    Delegates to the existing ``livekit.service`` module (room mgmt,
    in-call agent, composite egress recording). This is a thin adapter
    — no LiveKit SDK calls here.
    """

    name = "livekit"

    async def create(self, ctx: RequestContext, body: CreateMeetingRequest) -> ProviderCreateResult:
        """Reserve resources: compute deterministic room name.

        Do NOT create the LiveKit room yet. Rooms are created lazily on
        start() so they don't accumulate for meetings that never happen.
        """
        group_id = body.group_id
        if group_id:
            room_name = livekit_service.room_name_for_group(group_id)
            provider_payload = {"group_id": group_id, "room_name": room_name}
        else:
            room_name = f"meeting-{id(ctx)}"
            provider_payload = {"room_name": room_name}

        join_url = f"pocketpaw://meetings/{body.title or 'call'}?join"

        return ProviderCreateResult(
            provider_payload=provider_payload,
            join_url=join_url,
        )

    async def start(self, ctx: RequestContext, meeting) -> ProviderStartResult:
        """Create the LiveKit room + spawn the in-call agent.

        Idempotent: create_room is a no-op if the room already exists.
        """
        group_id = meeting.raw_provider_payload.get("group_id")
        if not group_id:
            raise ValueError("LiveKit start requires group_id in provider_payload")

        result = await livekit_service.create_room(group_id)

        return ProviderStartResult(
            provider_payload_updates={
                "started_at": datetime.now(UTC).isoformat(),
                "room_name": result.get("room_name", ""),
            },
            join_url=meeting.join_url,
        )

    async def cancel(self, ctx: RequestContext, meeting) -> None:
        """Nothing reserved server-side until start() — no-op."""
        return None

    async def end(self, ctx: RequestContext, meeting) -> None:
        """Stop agent + delete room."""
        group_id = meeting.raw_provider_payload.get("group_id")
        if group_id:
            await livekit_service.end_room(group_id)

    # ----- SupportsRecording -----

    async def request_recording(self, ctx: RequestContext, meeting) -> RecordingRef:
        """Start composite egress via the livekit recording module.

        Delegates to ``livekit_recording.start_composite_egress`` which wraps
        the existing LiveKit Egress API call.
        """
        group_id = meeting.raw_provider_payload.get("group_id")
        if not group_id:
            raise ValueError("LiveKit recording requires group_id in provider_payload")

        result = await livekit_recording.start_composite_egress(group_id)

        return RecordingRef(
            provider="livekit",
            external_id=result.get("egress_id", ""),
            status="recording",
            started_at=datetime.now(UTC),
            file_id=None,
        )

    async def stop_recording(self, ctx: RequestContext, meeting) -> None:
        """Stop active egress via the livekit recording module."""
        group_id = meeting.raw_provider_payload.get("group_id")
        if not group_id:
            return
        try:
            await livekit_recording.stop_egress(group_id)
        except RuntimeError:
            pass  # no active recording — idempotent
