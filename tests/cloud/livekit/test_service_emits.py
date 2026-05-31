"""Tests that ``livekit.service`` emits realtime ``call.*`` events.

The cloud livekit/ module owns the LiveKit room + meeting-notes agent
subprocess. State-mutating functions (``create_room``, ``end_room``,
``post_meeting_notes_to_group``) must emit a ``CallStarted`` / ``CallEnded``
/ ``CallNotesPosted`` event so the frontend's call panel + chat timeline
update live across every group member's tabs.

``generate_participant_token`` is intentionally *not* covered here — it's
an ephemeral JWT mint with no persistent state to broadcast.

Tests mock the ``LiveKitAPI`` async context manager (no real HTTP),
mock the agent subprocess spawn (no real child process), and assert on
``recording_bus.events``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud._core.realtime.events import (
    CallEnded,
    CallNotesPosted,
    CallStarted,
)


@pytest.fixture
def mock_lk_api():
    """Mock LiveKitAPI async context manager + LiveKit env vars."""
    from pocketpaw_ee.cloud.livekit.service import _active_agents

    _active_agents.clear()

    mock_room_svc = MagicMock()
    mock_api_instance = MagicMock()
    mock_api_instance.room = mock_room_svc
    mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
    mock_api_instance.__aexit__ = AsyncMock(return_value=False)

    patches = [
        patch("pocketpaw_ee.cloud.livekit.service.LiveKitAPI", return_value=mock_api_instance),
        patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud"),
        patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key"),
        patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret"),
    ]
    for p in patches:
        p.start()
    try:
        yield mock_room_svc
    finally:
        for p in patches:
            p.stop()
        _active_agents.clear()


async def test_create_room_emits_call_started(recording_bus, mock_lk_api) -> None:
    from pocketpaw_ee.cloud.livekit import service

    # create_room lists existing rooms first; return none so it takes the
    # create path. Both LiveKit room calls are awaited, so AsyncMock both.
    mock_lk_api.list_rooms = AsyncMock(return_value=MagicMock(rooms=[]))
    mock_lk_api.create_room = AsyncMock(return_value=MagicMock(name="group-call-g1"))

    # Pre-populate _active_agents so create_room skips the subprocess spawn path.
    service._active_agents["g1"] = MagicMock()

    await service.create_room("g1")

    started = [e for e in recording_bus.events if isinstance(e, CallStarted)]
    assert len(started) == 1
    ev = started[0]
    assert ev.type == "call.started"
    assert ev.data["group_id"] == "g1"
    assert ev.data["room_name"] == "group-call-g1"


async def test_end_room_emits_call_ended(recording_bus, mock_lk_api) -> None:
    from pocketpaw_ee.cloud.livekit import service

    mock_lk_api.delete_room = AsyncMock()

    await service.end_room("g1")

    ended = [e for e in recording_bus.events if isinstance(e, CallEnded)]
    assert len(ended) == 1
    ev = ended[0]
    assert ev.type == "call.ended"
    assert ev.data["group_id"] == "g1"
    assert ev.data["room_name"] == "group-call-g1"


async def test_end_room_emits_even_when_room_missing(recording_bus, mock_lk_api) -> None:
    """A stale end-call (already deleted upstream) still notifies peer tabs.

    The FE call panel must close even if the LiveKit room was already
    reaped on the LiveKit side; otherwise viewers stare at a dead room.
    """
    from pocketpaw_ee.cloud.livekit import service

    mock_lk_api.delete_room = AsyncMock(side_effect=Exception("room not found"))

    await service.end_room("g1")

    ended = [e for e in recording_bus.events if isinstance(e, CallEnded)]
    assert len(ended) == 1


async def test_post_meeting_notes_emits_call_notes_posted(recording_bus) -> None:
    from pocketpaw_ee.cloud.livekit import service

    fake_msg = MagicMock()
    fake_msg.id = "msg_abc"

    with (
        patch(
            "pocketpaw_ee.cloud.chat.message_service._create_group_message_doc",
            new_callable=AsyncMock,
            return_value=fake_msg,
        ),
        patch(
            "pocketpaw_ee.cloud.shared.events.event_bus.emit",
            new_callable=AsyncMock,
        ),
    ):
        await service.post_meeting_notes_to_group(
            group_id="g1",
            transcript="hi",
            summary="recap",
            action_items=["do thing"],
            participants=["Alice", "Bob"],
            duration_seconds=120,
        )

    posted = [e for e in recording_bus.events if isinstance(e, CallNotesPosted)]
    assert len(posted) == 1
    ev = posted[0]
    assert ev.type == "call.notes_posted"
    assert ev.data["group_id"] == "g1"
    assert ev.data["message_id"] == "msg_abc"
    assert ev.data["duration_seconds"] == 120
    assert ev.data["participant_count"] == 2
