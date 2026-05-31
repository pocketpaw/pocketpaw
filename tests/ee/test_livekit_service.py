"""Tests for the LiveKit call service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.livekit.service import (
    CALL_BOT_USER_ID,
    _active_agents,
    _format_duration,
    room_name_for_group,
)


@pytest.fixture(autouse=True)
def _install_recording_bus():
    """Install an inert bus so service-side emit() calls don't AssertionError.

    The shared cloud conftest.recording_bus fixture only covers tests/cloud/;
    tests/ee/ files like this one need their own. The fixture is autouse so
    every test in the module gets the bus without opting in.
    """
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _NullBus:
        async def publish(self, _event):
            return

        def subscribe(self, _event_type, _handler):
            return

    prev = bus_mod._bus
    bus_mod._bus = _NullBus()
    try:
        yield
    finally:
        bus_mod._bus = prev


class TestRoomNameForGroup:
    def test_generates_deterministic_name(self):
        name = room_name_for_group("abc123")
        assert name == "group-call-abc123"

    def test_uses_group_id(self):
        name = room_name_for_group("group_xyz")
        assert "group_xyz" in name


class TestFormatDuration:
    def test_seconds_only(self):
        assert _format_duration(30) == "30s"

    def test_minutes_and_seconds(self):
        assert _format_duration(125) == "2m 5s"

    def test_hours_minutes_seconds(self):
        assert _format_duration(3661) == "1h 1m 1s"

    def test_zero(self):
        assert _format_duration(0) == "0s"


class TestService:
    """Tests for LiveKit service functions (with mocked LiveKitAPI)."""

    @pytest.fixture
    def mock_lk_context(self):
        """Mock the LiveKitAPI async context manager at the import site.

        This prevents actual HTTP calls by replacing LiveKitAPI with a
        MagicMock that never makes real requests.
        """
        # Clean up any agents from previous tests
        _active_agents.clear()

        # Create the inner 'room' service mock
        mock_room_svc = MagicMock()

        # Mock list_rooms for is_new detection (returns empty by default)
        mock_list_resp = MagicMock()
        mock_list_resp.rooms = []
        mock_room_svc.list_rooms = AsyncMock(return_value=mock_list_resp)

        # Create the LiveKitAPI instance mock
        mock_api_instance = MagicMock()
        mock_api_instance.room = mock_room_svc
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)

        # Patch at the import site in the service module
        patcher = patch(
            "pocketpaw_ee.cloud.livekit.service.LiveKitAPI", return_value=mock_api_instance
        )
        patcher.start()

        yield mock_room_svc

        # Clean up any agents created during the test
        _active_agents.clear()
        patcher.stop()

    @pytest.mark.asyncio
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret")
    async def test_create_room(self, mock_lk_context):
        from pocketpaw_ee.cloud.livekit.service import create_room

        mock_room = MagicMock()
        mock_room.name = "group-call-test123"
        mock_lk_context.create_room = AsyncMock(return_value=mock_room)

        result = await create_room("test123")

        assert result["room_name"] == "group-call-test123"
        assert result["group_id"] == "test123"
        assert result["url"] == "wss://test.livekit.cloud"
        assert "bot_token" in result
        mock_lk_context.create_room.assert_called_once()

    @pytest.mark.asyncio
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret")
    async def test_generate_participant_token(self):
        from pocketpaw_ee.cloud.livekit.service import generate_participant_token

        token = await generate_participant_token(
            room_name="group-call-test123",
            identity="user_abc",
        )

        assert isinstance(token, str)
        assert len(token) > 0

    @pytest.mark.asyncio
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret")
    async def test_end_room(self, mock_lk_context):
        from pocketpaw_ee.cloud.livekit.service import end_room

        mock_lk_context.delete_room = AsyncMock()

        result = await end_room("test123")

        assert result["room_name"] == "group-call-test123"
        assert result["group_id"] == "test123"
        assert "ended_at" in result
        mock_lk_context.delete_room.assert_called_once()

    @pytest.mark.asyncio
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret")
    async def test_get_room_info_no_room(self, mock_lk_context):
        from pocketpaw_ee.cloud.livekit.service import get_room_info

        # Room list returns empty
        mock_list_resp = MagicMock()
        mock_list_resp.rooms = []
        mock_lk_context.list_rooms = AsyncMock(return_value=mock_list_resp)

        result = await get_room_info("test123")

        assert result is None

    @pytest.mark.asyncio
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret")
    async def test_get_room_info_with_participants(self, mock_lk_context):
        from pocketpaw_ee.cloud.livekit.service import get_room_info

        # Mock room list response with one room
        mock_room = MagicMock()
        mock_room.name = "group-call-test123"
        mock_list_resp = MagicMock()
        mock_list_resp.rooms = [mock_room]
        mock_lk_context.list_rooms = AsyncMock(return_value=mock_list_resp)

        # Mock participant list response
        mock_participant = MagicMock()
        mock_participant.identity = "user_abc"
        mock_participant.name = "Test User"
        mock_participant.kind = 0
        mock_participant.joined_at = None
        mock_parts_resp = MagicMock()
        mock_parts_resp.participants = [mock_participant]
        mock_lk_context.list_participants = AsyncMock(return_value=mock_parts_resp)

        result = await get_room_info("test123")

        assert result is not None
        assert result["active"] is True
        assert result["participant_count"] == 1
        assert result["participants"][0]["identity"] == "user_abc"

    @pytest.mark.asyncio
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret")
    async def test_not_configured_raises(self):
        from pocketpaw_ee.cloud.livekit.service import create_room

        with patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", ""):
            with pytest.raises(RuntimeError, match="LiveKit is not configured"):
                await create_room("test123")


class TestMeetingNotesAgent:
    """Tests for the CallMeetingAgent functionality."""

    @pytest.mark.asyncio
    @patch(
        "pocketpaw_ee.cloud.livekit.agent.CallMeetingAgent._finalize_notes", new_callable=AsyncMock
    )
    async def test_stop_generates_notes(self, mock_finalize):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="test123",
            room_name="group-call-test123",
            bot_token="test-token",
        )

        await agent.stop()

        mock_finalize.assert_called_once()

    def test_add_transcript_segment(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="test123",
            room_name="group-call-test123",
            bot_token="test-token",
        )

        agent.add_transcript_segment("Alice", "Hello everyone")
        agent.add_transcript_segment("Bob", "Hi Alice")

        assert len(agent.transcript_segments) == 2
        assert agent.transcript_segments[0]["speaker"] == "Alice"
        assert agent.transcript_segments[1]["text"] == "Hi Alice"

    def test_parse_summary_json(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="test123",
            room_name="group-call-test123",
            bot_token="test-token",
        )

        content = '{"summary": "We discussed X", "action_items": ["Do Y", "Do Z"]}'
        summary, items = agent._parse_summary_json(content)

        assert summary == "We discussed X"
        assert items == ["Do Y", "Do Z"]

    def test_parse_summary_json_markdown_block(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="test123",
            room_name="group-call-test123",
            bot_token="test-token",
        )

        content = '```json\n{"summary": "Summary", "action_items": ["Item 1"]}\n```'
        summary, items = agent._parse_summary_json(content)

        assert summary == "Summary"
        assert items == ["Item 1"]

    def test_parse_summary_json_fallback(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="test123",
            room_name="group-call-test123",
            bot_token="test-token",
        )

        content = "Plain text summary with no JSON structure"
        summary, items = agent._parse_summary_json(content)

        assert summary == content
        assert items == []

    def test_heuristic_summary(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="test123",
            room_name="group-call-test123",
            bot_token="test-token",
        )

        transcript = "Line 1\nLine 2\nLine 3"
        summary, items = agent._summarize_heuristic(transcript)

        assert "Line 1" in summary
        assert items == []


class TestMeetingNotesPosting:
    """Tests for posting meeting notes to groups."""

    @pytest.mark.asyncio
    async def test_post_meeting_notes(self):
        from pocketpaw_ee.cloud.livekit.service import post_meeting_notes_to_group

        with (
            patch(
                "pocketpaw_ee.cloud.chat.message_service._create_group_message_doc",
                new_callable=AsyncMock,
            ) as mock_create,
            patch(
                "pocketpaw_ee.cloud.shared.events.event_bus.emit",
                new_callable=AsyncMock,
            ) as mock_emit,
        ):
            mock_create.return_value.id = "msg_123"

            await post_meeting_notes_to_group(
                group_id="test123",
                transcript="Hello world",
                summary="A summary",
                action_items=["Item 1"],
                participants=["Alice"],
                duration_seconds=120,
            )

            mock_create.assert_called_once()
            assert mock_create.call_args[1]["group_id"] == "test123"
            assert "Meeting Notes" in mock_create.call_args[1]["content"]
            assert "A summary" in mock_create.call_args[1]["content"]
            assert "Item 1" in mock_create.call_args[1]["content"]
            mock_emit.assert_called_once_with(
                "message.sent",
                {
                    "group_id": "test123",
                    "message_id": "msg_123",
                    "sender_id": CALL_BOT_USER_ID,
                    "sender_type": "user",
                    "content": mock_create.call_args[1]["content"],
                    "mentions": [],
                },
            )

    @pytest.mark.asyncio
    async def test_post_meeting_notes_empty_transcript(self):
        from pocketpaw_ee.cloud.livekit.service import post_meeting_notes_to_group

        with (
            patch(
                "pocketpaw_ee.cloud.chat.message_service._create_group_message_doc",
                new_callable=AsyncMock,
            ) as mock_create,
            patch(
                "pocketpaw_ee.cloud.shared.events.event_bus.emit",
                new_callable=AsyncMock,
            ) as mock_emit,
        ):
            mock_create.return_value.id = "msg_456"

            await post_meeting_notes_to_group(
                group_id="test123",
                transcript="",
                summary="No speech detected.",
                action_items=[],
                participants=[],
                duration_seconds=30,
            )

            mock_create.assert_called_once()
            assert "No speech detected" in mock_create.call_args[1]["content"]
            mock_emit.assert_called_once()
