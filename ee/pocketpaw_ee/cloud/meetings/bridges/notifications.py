"""Meeting events → in-app notification fan-out.

Subscribes to the meeting.* topics on ``shared.events.event_bus`` and
turns each into a notification via ``notifications_service.create``. The
notification ``kind`` strings (``meeting_scheduled`` / ``meeting_cancelled``
/ ``meeting_started`` / ``meeting_reminder`` / ``meeting_recording_ready``
/ ``meeting_transcript_ready``) match what the paw-enterprise frontend
listens for, so wiring the subscriber is all it takes to light up the
in-app toast + notification bell for every source.

This bridge is source-agnostic — it reads ``provider`` (or ``source``)
off the event payload only to enrich the notification body text. The
fan-out logic doesn't fork.

Phase 1 scope: recipient is the meeting's creator (the user who fired
``create_meeting``). Participant fan-out lands when meetings grow a
participant_user_ids list (Phase 3 alongside LiveKit scheduling).
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.notifications.domain import NotificationSource
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handlers — one per event type. Each pulls the recipient + a friendly
# title/body and delegates to notifications_service.create.
# ---------------------------------------------------------------------------


async def _on_meeting_scheduled(data: dict[str, Any]) -> None:
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    if not (workspace_id and meeting_id):
        return

    source = data.get("source", "recall")

    # LiveKit meetings: fan-out to ALL group members so everyone in the
    # channel sees the notification. Recall meetings: creator-only (legacy).
    if source == "livekit":
        group_id = data.get("group_id")
        if group_id:
            recipients = await _group_member_ids(workspace_id, group_id)
        else:
            recipients = []
    else:
        creator = data.get("created_by") or data.get("organizer_user_id")
        recipients = [creator] if creator else []

    if not recipients:
        return

    provider = data.get("provider") or source or "meeting"
    for recipient in recipients:
        await _create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind="meeting_scheduled",
            title="Meeting scheduled",
            body=f"A {_pretty(provider)} meeting was scheduled.",
            meeting_id=meeting_id,
        )


async def _on_meeting_started(data: dict[str, Any]) -> None:
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    if not (workspace_id and meeting_id):
        return

    source = data.get("source", "recall")
    group_id = data.get("group_id")
    if source == "livekit" and group_id:
        recipients = await _group_member_ids(workspace_id, group_id)
    else:
        creator = data.get("created_by") or data.get("organizer_user_id")
        recipients = [creator] if creator else []

    if not recipients:
        return

    for recipient in recipients:
        await _create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind="meeting_started",
            title="Meeting started",
            body="A meeting has started.",
            meeting_id=meeting_id,
            room_id=group_id,
        )


async def _on_meeting_cancelled(data: dict[str, Any]) -> None:
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    recipient = data.get("cancelled_by_user_id") or data.get("created_by")
    if not (workspace_id and meeting_id and recipient):
        return

    await _create(
        workspace_id=workspace_id,
        recipient=recipient,
        kind="meeting_cancelled",
        title="Meeting cancelled",
        body="A scheduled meeting was cancelled.",
        meeting_id=meeting_id,
    )


async def _on_meeting_recording_ready(data: dict[str, Any]) -> None:
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    recipient = data.get("organizer_user_id") or data.get("created_by")
    if not (workspace_id and meeting_id and recipient):
        return

    await _create(
        workspace_id=workspace_id,
        recipient=recipient,
        kind="meeting_recording_ready",
        title="Recording ready",
        body="The meeting recording is ready to view.",
        meeting_id=meeting_id,
    )


async def _on_meeting_transcript_ready(data: dict[str, Any]) -> None:
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    recipient = data.get("organizer_user_id") or data.get("created_by")
    if not (workspace_id and meeting_id and recipient):
        return

    await _create(
        workspace_id=workspace_id,
        recipient=recipient,
        kind="meeting_transcript_ready",
        title="Transcript ready",
        body="The meeting transcript is ready to read.",
        meeting_id=meeting_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pretty(provider: str) -> str:
    """Friendly capitalisation for the notification body."""
    return {
        "zoom": "Zoom",
        "google_meet": "Google Meet",
        "livekit": "LiveKit",
        "recall": "meeting",
    }.get(provider, provider)


async def _group_member_ids(workspace_id: str, group_id: str) -> list[str]:
    """List user ids for all members of a group. Returns [] on any error."""
    try:
        from pocketpaw_ee.cloud.chat import group_service

        return await group_service.list_member_ids(group_id)
    except Exception:
        logger.exception("Failed to list members for group=%s", group_id)
        return []


async def _create(
    *,
    workspace_id: str,
    recipient: str,
    kind: str,
    title: str,
    body: str,
    meeting_id: str,
    room_id: str | None = None,
) -> None:
    """Wrap notifications_service.create — late import to avoid circular
    deps and tolerate the service being unavailable in unit-test contexts."""
    try:
        from pocketpaw_ee.cloud.notifications import service as notifications_service

        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind=kind,
            title=title,
            body=body,
            # type=kind so the frontend's sourceUrl switch can deep-link
            # (meeting_started → ?join=meeting-{id}, meeting_reminder → chat, etc.)
            source=NotificationSource(type=kind, id=meeting_id, room_id=room_id),
        )
    except Exception:
        logger.exception("Failed to create %s notification for meeting=%s", kind, meeting_id)


# ---------------------------------------------------------------------------
# Registration — called from mount_cloud() after init_realtime.
# ---------------------------------------------------------------------------


def register_meeting_notification_listeners() -> None:
    """Wire the meeting.* → notification subscribers. Idempotent."""
    event_bus.subscribe("meeting.scheduled", _on_meeting_scheduled)
    event_bus.subscribe("meeting.started", _on_meeting_started)
    event_bus.subscribe("meeting.cancelled", _on_meeting_cancelled)
    event_bus.subscribe("meeting.recording_ready", _on_meeting_recording_ready)
    event_bus.subscribe("meeting.transcript_ready", _on_meeting_transcript_ready)
    logger.info("registered meeting.* → notifications subscribers")


__all__ = ["register_meeting_notification_listeners"]
