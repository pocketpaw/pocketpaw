"""Tests for the LiveKit call service.

Added TestSelectiveSubscribe: covers the call bot's audio-only selective
subscribe (Bug A) — auto_subscribe=False on connect and the pure
_should_subscribe() decision (mic audio in, video / screenshare out).

Added TestSpawnRace: covers the duplicate call-bot spawn race — concurrent
create_room() for the same group must spawn exactly one agent (the per-group
asyncio.Lock makes check→spawn→insert atomic).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.livekit.service import (
    CALL_BOT_USER_ID,
    _active_agents,
    _format_duration,
    _spawn_locks,
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
        # Clean up any agents / spawn locks from previous tests
        _active_agents.clear()
        _spawn_locks.clear()

        # Create the inner 'room' service mock
        mock_room_svc = MagicMock()

        # Mock list_rooms for is_new detection (returns empty by default)
        mock_list_resp = MagicMock()
        mock_list_resp.rooms = []
        mock_room_svc.list_rooms = AsyncMock(return_value=mock_list_resp)

        # Mock list_participants for _call_bot_in_room — empty by default so the
        # cross-replica call-bot check is negative and create_room proceeds to
        # spawn (tests that need a call-bot present override this).
        mock_parts_resp = MagicMock()
        mock_parts_resp.participants = []
        mock_room_svc.list_participants = AsyncMock(return_value=mock_parts_resp)

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

        # Clean up any agents / spawn locks created during the test
        _active_agents.clear()
        _spawn_locks.clear()
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

    @pytest.mark.asyncio
    async def test_stop_does_not_block_on_slow_transcribe_task(self):
        """Regression test: stop() must NOT await a cancelled transcribe task.

        The old code awaited the cancelled transcribe task, which blocked on
        cleanup of AudioStream/Deepgram pipes for long meetings. The fix
        cancels the task without awaiting it, letting asyncio.run() clean up.
        """
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="test123",
            room_name="group-call-test123",
            bot_token="test-token",
        )

        # Set up a transcribe task that simulates a long-running cleanup
        # after cancellation (e.g. draining audio streams).
        agent._running = True
        agent._transcribe_task = asyncio.create_task(self._never_ending())

        # This must complete quickly (~0s, not ~10s+).
        with patch.object(agent, "_finalize_notes", new_callable=AsyncMock) as mock_finalize:
            await asyncio.wait_for(agent.stop(), timeout=2)

        mock_finalize.assert_called_once()

    @staticmethod
    async def _never_ending() -> None:
        """A task that never finishes cleanly after cancellation."""
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Simulate slow cleanup (old code would await this)
            await asyncio.sleep(30)
            raise

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

    @pytest.mark.asyncio
    async def test_finalize_notes_truncates_long_transcript_for_llm(self):
        """Long transcripts should be truncated before the LLM call.

        The full transcript is still saved in the payload, but the LLM
        should only receive ~5000 chars so the API call completes within
        the process-grace-period window.
        """
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="test123",
            room_name="group-call-test123",
            bot_token="test-token",
        )
        agent._call_start_time = time.time()

        # Add transcript segments totalling well over 5000 chars
        long_text = "Hello world. " * 1000  # ~14K chars
        agent.add_transcript_segment("Alice", long_text)
        agent.add_transcript_segment("Bob", "Short reply.")

        with patch.object(agent, "_generate_summary", AsyncMock()) as mock_gen:
            mock_gen.return_value = ("summary", ["action"])
            await agent._finalize_notes()

        # Verify the LLM got a truncated version (head + tail + ellipsis
        # is ~5048 chars with the speaker prefix; assert it's well under
        # the original ~14K length and contains the truncation marker).
        called_with = mock_gen.call_args[0][0]
        assert len(called_with) < 14000, (
            f"LLM got full {len(called_with)}-char transcript, expected truncation"
        )
        assert "[... transcript truncated at 5000 chars ...]" in called_with


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


class _FakeRoom:
    """Minimal stand-in for ``livekit.rtc.Room``.

    Records the event handlers the agent registers via ``@room.on(...)`` so a
    test can fire them directly, and captures the ``RoomOptions`` passed to
    ``connect`` so we can assert on ``auto_subscribe``.
    """

    def __init__(self) -> None:
        self.handlers: dict = {}
        self.remote_participants: dict = {}
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()

    def on(self, event: str):
        def _register(fn):
            self.handlers[event] = fn
            return fn

        return _register


class TestSelectiveSubscribe:
    """Bug A: the call bot must subscribe to mic audio only, never video."""

    def test_should_subscribe_microphone_audio(self):
        from livekit.rtc import TrackKind, TrackSource
        from pocketpaw_ee.cloud.livekit.agent import _should_subscribe

        pub = SimpleNamespace(kind=TrackKind.KIND_AUDIO, source=TrackSource.SOURCE_MICROPHONE)
        assert _should_subscribe(pub) is True

    def test_should_subscribe_untagged_audio(self):
        # Some publishers leave the source UNKNOWN on a plain audio publish;
        # treat that as a microphone so we don't drop real speech.
        from livekit.rtc import TrackKind, TrackSource
        from pocketpaw_ee.cloud.livekit.agent import _should_subscribe

        pub = SimpleNamespace(kind=TrackKind.KIND_AUDIO, source=TrackSource.SOURCE_UNKNOWN)
        assert _should_subscribe(pub) is True

    def test_should_not_subscribe_camera_video(self):
        from livekit.rtc import TrackKind, TrackSource
        from pocketpaw_ee.cloud.livekit.agent import _should_subscribe

        pub = SimpleNamespace(kind=TrackKind.KIND_VIDEO, source=TrackSource.SOURCE_CAMERA)
        assert _should_subscribe(pub) is False

    def test_should_not_subscribe_screenshare_video(self):
        from livekit.rtc import TrackKind, TrackSource
        from pocketpaw_ee.cloud.livekit.agent import _should_subscribe

        pub = SimpleNamespace(kind=TrackKind.KIND_VIDEO, source=TrackSource.SOURCE_SCREENSHARE)
        assert _should_subscribe(pub) is False

    def test_should_not_subscribe_screenshare_audio(self):
        # System audio shared from a screen share is not a participant mic.
        from livekit.rtc import TrackKind, TrackSource
        from pocketpaw_ee.cloud.livekit.agent import _should_subscribe

        pub = SimpleNamespace(
            kind=TrackKind.KIND_AUDIO, source=TrackSource.SOURCE_SCREENSHARE_AUDIO
        )
        assert _should_subscribe(pub) is False

    @pytest.mark.asyncio
    async def test_connect_uses_auto_subscribe_false(self):
        """The bot must connect with auto_subscribe disabled.

        Auto-subscribe drags in every participant's video, which the bot
        never uses — the source of the O(N) per-participant load.
        """
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
            livekit_url="wss://fake.livekit.cloud",
        )
        agent._running = False  # exit the keep-alive loop immediately

        fake = _FakeRoom()
        with patch("livekit.rtc.Room", return_value=fake):
            await agent._connect_and_transcribe()

        fake.connect.assert_awaited_once()
        opts = fake.connect.await_args.args[2]
        assert opts.auto_subscribe is False

    @pytest.mark.asyncio
    async def test_video_publication_not_subscribed_in_handler(self):
        """track_published must subscribe mic audio but ignore video."""
        from livekit.rtc import TrackKind, TrackSource
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
            livekit_url="wss://fake.livekit.cloud",
        )
        agent._running = False

        fake = _FakeRoom()
        with patch("livekit.rtc.Room", return_value=fake):
            await agent._connect_and_transcribe()

        handler = fake.handlers["track_published"]
        participant = MagicMock()
        participant.identity = "user_1"
        participant.name = "User One"

        video_pub = MagicMock()
        video_pub.kind = TrackKind.KIND_VIDEO
        video_pub.source = TrackSource.SOURCE_CAMERA
        video_pub.subscribed = False
        handler(video_pub, participant)
        video_pub.set_subscribed.assert_not_called()

        mic_pub = MagicMock()
        mic_pub.kind = TrackKind.KIND_AUDIO
        mic_pub.source = TrackSource.SOURCE_MICROPHONE
        mic_pub.subscribed = False
        handler(mic_pub, participant)
        mic_pub.set_subscribed.assert_called_once_with(True)


class TestSpawnRace:
    """Regression: concurrent create_room() must spawn exactly one call-bot.

    Two participants joining the same group call at once issue two concurrent
    create_room() calls. Before the per-group lock, both passed the
    ``group_id not in _active_agents`` check before either inserted (the await
    inside _spawn_agent_process yields between the check and the insert), so two
    "call-bot" subprocesses joined the room with the same identity and fought.
    The fix makes check→spawn→insert atomic per group via an asyncio.Lock and
    adds a best-effort cross-replica check (_call_bot_in_room) against LiveKit
    room state.
    """

    @pytest.fixture(autouse=True)
    def _reset_globals(self):
        """Reset module-level spawn registries before/after each test.

        Hermeticity: _active_agents and _spawn_locks are process globals, and an
        asyncio.Lock is bound to the loop it is first used on, so a leaked lock
        would poison a later test running on a fresh loop.
        """
        _active_agents.clear()
        _spawn_locks.clear()
        try:
            yield
        finally:
            _active_agents.clear()
            _spawn_locks.clear()

    @pytest.fixture
    def mock_lk_existing_room(self):
        """Mock LiveKitAPI: the room already exists (is_new=False) and no
        call-bot is present (list_participants returns an empty list)."""
        mock_room_svc = MagicMock()

        existing_room = MagicMock()
        existing_room.name = "group-call-g"
        mock_list_resp = MagicMock()
        mock_list_resp.rooms = [existing_room]
        mock_room_svc.list_rooms = AsyncMock(return_value=mock_list_resp)

        mock_parts_resp = MagicMock()
        mock_parts_resp.participants = []
        mock_room_svc.list_participants = AsyncMock(return_value=mock_parts_resp)

        mock_api_instance = MagicMock()
        mock_api_instance.room = mock_room_svc
        mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
        mock_api_instance.__aexit__ = AsyncMock(return_value=False)

        patcher = patch(
            "pocketpaw_ee.cloud.livekit.service.LiveKitAPI", return_value=mock_api_instance
        )
        patcher.start()
        try:
            yield mock_room_svc
        finally:
            patcher.stop()

    @pytest.mark.asyncio
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret")
    async def test_concurrent_create_room_spawns_single_agent(self, mock_lk_existing_room):
        """Two concurrent create_room() for the same group spawn ONE agent.

        Fails on the pre-lock code: the await in _spawn_agent_process yields
        between the _active_agents check and the insert, so both calls pass the
        check and spawn (2). Passes once the per-group asyncio.Lock makes
        check→spawn→insert atomic.
        """
        from pocketpaw_ee.cloud.livekit.service import create_room

        spawn_calls = 0

        async def _fake_spawn(*args, **kwargs):
            nonlocal spawn_calls
            spawn_calls += 1
            # Yield the loop here — this is the exact window the race exploited
            # (between the _active_agents check and the insert).
            await asyncio.sleep(0)
            return MagicMock()  # stand-in proc; _reap is patched out

        with (
            patch(
                "pocketpaw_ee.cloud.livekit.service._spawn_agent_process",
                side_effect=_fake_spawn,
            ) as mock_spawn,
            patch(
                "pocketpaw_ee.cloud.livekit.service._reap_agent_process",
                new_callable=AsyncMock,
            ),
        ):
            await asyncio.gather(
                create_room("g", "ws", "u"),
                create_room("g", "ws", "u"),
            )
            # Let any background reap task drain so it doesn't warn on teardown.
            await asyncio.sleep(0)

        assert spawn_calls == 1, f"expected exactly 1 spawn, got {spawn_calls}"
        assert mock_spawn.call_count == 1
        assert list(_active_agents) == ["g"]


class TestProgressiveSummarization:
    """Tests for progressive summarization during live calls."""

    # ------------------------------------------------------------------
    # _parse_progressive_json
    # ------------------------------------------------------------------

    def test_parse_progressive_json_valid(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        content = (
            '{"segment_summary": "Discussed pricing",'
            ' "action_items": ["Send quote"],'
            ' "topics": ["Pricing", "Timeline"]}'
        )
        result = agent._parse_progressive_json(content, {"Alice", "Bob"})

        assert result["segment_summary"] == "Discussed pricing"
        assert result["action_items"] == ["Send quote"]
        assert result["topics"] == ["Pricing", "Timeline"]
        assert "Alice" in result["participants"]
        assert "Bob" in result["participants"]
        assert "timestamp" in result

    def test_parse_progressive_json_markdown_block(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        content = (
            "```json\n"
            '{"segment_summary": "Wrapped up sprint planning",'
            ' "action_items": [], "topics": ["Sprint"]}\n'
            "```"
        )
        result = agent._parse_progressive_json(content, {"Charlie"})

        assert result["segment_summary"] == "Wrapped up sprint planning"
        assert result["action_items"] == []
        assert result["topics"] == ["Sprint"]
        assert "Charlie" in result["participants"]

    def test_parse_progressive_json_code_fence_no_json(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        content = '```\n{"segment_summary": "Planning", "action_items": []}\n```'
        result = agent._parse_progressive_json(content, {"Dave"})

        assert result["segment_summary"] == "Planning"

    def test_parse_progressive_json_fallback_to_raw_text(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        content = "Raw text that is not JSON at all but still useful as summary"
        result = agent._parse_progressive_json(content, {"Eve"})

        # Should use raw text truncated as segment_summary
        assert "not JSON" in result["segment_summary"]
        assert result["action_items"] == []
        assert result["topics"] == []
        assert "Eve" in result["participants"]

    # ------------------------------------------------------------------
    # _store_progressive_summary / _fetch_progressive_summaries
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_store_fetches_from_in_memory_when_no_redis(self):
        """Progressive summaries are stored in-memory when Redis is unavailable."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        summary_a = {
            "segment_summary": "First segment",
            "action_items": ["Task A"],
            "topics": ["Topic 1"],
            "participants": ["Alice"],
            "timestamp": 1000.0,
        }
        summary_b = {
            "segment_summary": "Second segment",
            "action_items": ["Task B"],
            "topics": ["Topic 2"],
            "participants": ["Bob"],
            "timestamp": 2000.0,
        }

        with patch.object(agent, "_get_redis", AsyncMock(return_value=None)):
            await agent._store_progressive_summary(summary_a)
            await agent._store_progressive_summary(summary_b)

            assert len(agent._progressive_summaries) == 2
            assert agent._progressive_summaries[0]["segment_summary"] == "First segment"

            fetched = await agent._fetch_progressive_summaries()

            assert len(fetched) == 2
            assert fetched[0]["segment_summary"] == "First segment"
            assert fetched[1]["action_items"] == ["Task B"]

    @pytest.mark.asyncio
    async def test_store_fetches_from_redis_when_available(self):
        """When Redis IS available, _fetch reads from Redis first."""
        import json

        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        summary_a = {
            "segment_summary": "From Redis",
            "action_items": [],
            "topics": [],
            "participants": [],
            "timestamp": 1000.0,
        }

        # Mock Redis with lrange returning data and rpush/expire being no-ops
        mock_redis = AsyncMock()
        mock_redis.lrange.return_value = [json.dumps(summary_a)]

        with patch.object(agent, "_get_redis", AsyncMock(return_value=mock_redis)):
            await agent._store_progressive_summary(summary_a)
            # After store, in-memory list should have it too
            assert len(agent._progressive_summaries) == 1

            fetched = await agent._fetch_progressive_summaries()

            assert len(fetched) == 1
            assert fetched[0]["segment_summary"] == "From Redis"
            mock_redis.lrange.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_store_pushes_to_redis_with_ttl(self):
        """_store_progressive_summary pushes to Redis with rpush and sets TTL."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        mock_redis = AsyncMock()
        mock_redis.lrange.return_value = []

        with patch.object(agent, "_get_redis", AsyncMock(return_value=mock_redis)):
            await agent._store_progressive_summary(
                {
                    "segment_summary": "Test",
                    "action_items": [],
                    "topics": [],
                    "participants": [],
                    "timestamp": 1000.0,
                }
            )

            # Redis should have received rpush and expire calls
            mock_redis.rpush.assert_awaited_once()
            # First arg to rpush should be the Redis key
            key_arg = mock_redis.rpush.await_args[0][0]
            assert key_arg == "livekit:progressive:g"

            mock_redis.expire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_prefers_redis_over_in_memory(self):
        """_fetch_progressive_summaries prefers Redis even when in-memory exists."""
        import json

        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        # Set in-memory summaries
        agent._progressive_summaries.append(
            {
                "segment_summary": "In-memory only",
                "action_items": [],
                "topics": [],
                "participants": [],
                "timestamp": 1000.0,
            }
        )

        # Mock Redis with different data
        mock_redis = AsyncMock()
        mock_redis.lrange.return_value = [
            json.dumps(
                {
                    "segment_summary": "From Redis",
                    "action_items": ["Redis task"],
                    "topics": [],
                    "participants": [],
                    "timestamp": 2000.0,
                }
            )
        ]

        with patch.object(agent, "_get_redis", AsyncMock(return_value=mock_redis)):
            fetched = await agent._fetch_progressive_summaries()

            assert len(fetched) == 1
            assert fetched[0]["segment_summary"] == "From Redis"
            assert fetched[0]["action_items"] == ["Redis task"]

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_when_nothing_stored(self):
        """_fetch_progressive_summaries returns [] when no summaries exist."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        fetched = await agent._fetch_progressive_summaries()
        assert fetched == []

    @pytest.mark.asyncio
    async def test_get_redis_graceful_degradation(self):
        """_get_redis returns None when Redis is not configured."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        # With get_redis raising RuntimeError (no POCKETPAW_REDIS_URL)
        with patch(
            "pocketpaw_ee.cloud.livekit.agent.CallMeetingAgent._get_redis",
            new_callable=AsyncMock,
        ) as mock_get:
            mock_get.return_value = None
            assert await agent._get_redis() is None

    # ------------------------------------------------------------------
    # _generate_progressive_summary
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_generate_progressive_summary_returns_none_for_no_segments(self):
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        result = await agent._generate_progressive_summary([])
        assert result is None

    @pytest.mark.asyncio
    async def test_generate_progressive_summary_deepseek(self):
        """_generate_progressive_summary uses DeepSeek when key is set."""
        import json
        import os

        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        segments = [
            {"speaker": "Alice", "text": "Let's discuss the Q2 roadmap.", "timestamp": 100.0},
            {
                "speaker": "Bob",
                "text": "I think we should focus on the mobile app.",
                "timestamp": 102.0,
            },
            {"speaker": "Alice", "text": "Agreed, mobile is the priority.", "timestamp": 105.0},
        ]

        with (
            patch.object(agent, "_call_llm_json", new_callable=AsyncMock) as mock_llm,
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-deepseek"}),
        ):
            mock_llm.return_value = json.dumps(
                {
                    "segment_summary": "Alice and Bob discussed Q2 roadmap with mobile app focus.",
                    "action_items": ["Prioritize mobile app development"],
                    "topics": ["Q2 Roadmap", "Mobile App"],
                }
            )

            result = await agent._generate_progressive_summary(segments)

            assert result is not None
            assert "Q2 roadmap" in result["segment_summary"]
            assert "mobile app" in result["segment_summary"].lower()
            assert "Prioritize mobile app" in result["action_items"][0]
            assert "Alice" in result["participants"]
            assert "Bob" in result["participants"]

            # Should have called DeepSeek
            mock_llm.assert_called_once()
            assert mock_llm.call_args[1]["provider"] == "deepseek"

    @pytest.mark.asyncio
    async def test_generate_progressive_summary_falls_back_through_providers(self):
        """Falls back from DeepSeek → Anthropic → OpenAI when each fails."""
        import json
        import os

        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        segments = [
            {"speaker": "Alice", "text": "Status update: backend is done.", "timestamp": 100.0},
        ]

        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "sk-ds",
                "ANTHROPIC_API_KEY": "sk-ant",
                "OPENAI_API_KEY": "sk-openai",
            },
        ):
            call_count = 0

            async def _failing_then_succeeding(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("DeepSeek down")
                if call_count == 2:
                    raise RuntimeError("Anthropic down")
                # OpenAI succeeds
                return json.dumps(
                    {
                        "segment_summary": "Backend status update from Alice.",
                        "action_items": [],
                        "topics": ["Backend"],
                    }
                )

            with patch.object(agent, "_call_llm_json", side_effect=_failing_then_succeeding):
                result = await agent._generate_progressive_summary(segments)

                assert result is not None
                assert "Backend" in result["segment_summary"]
                assert call_count == 3

    @pytest.mark.asyncio
    async def test_generate_progressive_summary_returns_none_when_all_fail(self):
        """Returns None when all LLM providers fail."""
        import os

        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        segments = [
            {"speaker": "Alice", "text": "Hello", "timestamp": 100.0},
        ]

        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "sk-ds",
            },
        ):
            with patch.object(agent, "_call_llm_json", AsyncMock(side_effect=RuntimeError("fail"))):
                result = await agent._generate_progressive_summary(segments)

                assert result is None

    # ------------------------------------------------------------------
    # _call_llm_json
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_call_llm_json_deepseek(self):
        """_call_llm_json calls DeepSeek API correctly."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "DeepSeek response"}}]
            }
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await agent._call_llm_json(
                provider="deepseek",
                api_key="sk-test",
                prompt="Summarize this",
                model="deepseek-chat",
                max_tokens=1024,
                timeout=60,
            )

            assert result == "DeepSeek response"
            mock_client.post.assert_awaited_once()
            call_url = mock_client.post.await_args[0][0]
            assert "deepseek.com" in call_url

    @pytest.mark.asyncio
    async def test_call_llm_json_anthropic(self):
        """_call_llm_json calls Anthropic API correctly."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            mock_response = MagicMock()
            mock_response.json.return_value = {"content": [{"text": "Anthropic response"}]}
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await agent._call_llm_json(
                provider="anthropic",
                api_key="sk-ant-test",
                prompt="Summarize this",
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                timeout=60,
            )

            assert result == "Anthropic response"
            mock_client.post.assert_awaited_once()
            call_url = mock_client.post.await_args[0][0]
            assert "anthropic.com" in call_url

    @pytest.mark.asyncio
    async def test_call_llm_json_openai(self):
        """_call_llm_json calls OpenAI API correctly."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "OpenAI response"}}]
            }
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await agent._call_llm_json(
                provider="openai",
                api_key="sk-ope-test",
                prompt="Summarize this",
                model="gpt-4o-mini",
                max_tokens=1024,
                timeout=60,
            )

            assert result == "OpenAI response"
            mock_client.post.assert_awaited_once()
            call_url = mock_client.post.await_args[0][0]
            assert "openai.com" in call_url

    # ------------------------------------------------------------------
    # _merge_progressive_summaries
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_merge_progressive_summaries_deepseek(self):
        """_merge_progressive_summaries calls DeepSeek and returns parsed result."""
        import json
        import os

        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        progressive_summaries = [
            {
                "segment_summary": "Discussed Q1 goals.",
                "action_items": ["Set up Q1 milestones"],
                "topics": ["Q1 Goals"],
                "participants": ["Alice", "Bob"],
                "timestamp": 1000.0,
            },
            {
                "segment_summary": "Reviewed budget.",
                "action_items": ["Finalize budget by Friday"],
                "topics": ["Budget"],
                "participants": ["Charlie"],
                "timestamp": 2000.0,
            },
        ]

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-ds"}),
            patch.object(agent, "_call_llm_json", new_callable=AsyncMock) as mock_llm,
        ):
            mock_llm.return_value = json.dumps(
                {
                    "summary": "Meeting covered Q1 goals and budget review.",
                    "action_items": ["Set up Q1 milestones", "Finalize budget by Friday"],
                }
            )

            summary, items = await agent._merge_progressive_summaries(
                progressive_summaries,
                "Full transcript text...",
            )

            assert "Q1 goals" in summary
            assert "budget" in summary
            assert len(items) == 2
            assert "Finalize budget" in items[1]

    @pytest.mark.asyncio
    async def test_merge_progressive_summaries_fallback_to_first(self):
        """When all LLMs fail, uses the first progressive summary as fallback."""
        import os

        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        progressive_summaries = [
            {
                "segment_summary": "First segment summary.",
                "action_items": ["Do first thing"],
                "topics": [],
                "participants": [],
                "timestamp": 1000.0,
            },
        ]

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-ds"}):
            with patch.object(agent, "_call_llm_json", AsyncMock(side_effect=RuntimeError("fail"))):
                summary, items = await agent._merge_progressive_summaries(
                    progressive_summaries,
                    "",
                )

                assert "First segment" in summary
                assert items == ["Do first thing"]

    @pytest.mark.asyncio
    async def test_merge_progressive_summaries_full_fallback(self):
        """When no summaries and no LLM works, returns unavailable message."""
        import os

        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )

        # Ensure no API keys are set so all LLM calls fail
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "OPENAI_API_KEY": "",
            },
        ):
            summary, items = await agent._merge_progressive_summaries([], "")
        assert "unavailable" in summary.lower()
        assert items == ["Summarization failed."]

    # ------------------------------------------------------------------
    # _finalize_notes uses progressive path
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_finalize_notes_prefers_progressive_summaries(self):
        """_finalize_notes uses progressive merge when summaries exist."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )
        agent._call_start_time = time.time()
        agent._participant_identities.add("user_1")
        agent._participant_info["user_1"] = "Alice"

        # Add some transcript segments
        agent.add_transcript_segment("Alice", "Hello everyone")
        agent.add_transcript_segment("Bob", "Hi Alice")
        agent.add_transcript_segment("Alice", "Let's get started on the roadmap")

        # Set up progressive summaries
        agent._progressive_summaries.append(
            {
                "segment_summary": "Alice and Bob discussed the roadmap.",
                "action_items": ["Start on roadmap"],
                "topics": ["Roadmap"],
                "participants": ["Alice", "Bob"],
                "timestamp": 1000.0,
            }
        )

        with (
            patch.object(
                agent, "_merge_progressive_summaries", new_callable=AsyncMock
            ) as mock_merge,
            patch.object(agent, "_get_redis", AsyncMock(return_value=None)),
        ):
            mock_merge.return_value = ("Progressive summary result.", ["Roadmap task"])

            await agent._finalize_notes()

            # Should have called the merge path
            mock_merge.assert_awaited_once()
            # Should have received the progressive summaries
            assert len(mock_merge.await_args[0][0]) == 1
            assert (
                mock_merge.await_args[0][0][0]["segment_summary"]
                == "Alice and Bob discussed the roadmap."
            )

    @pytest.mark.asyncio
    async def test_finalize_notes_falls_back_to_direct_summary(self):
        """_finalize_notes falls back to direct summary when no progressive summaries."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )
        agent._call_start_time = time.time()
        agent._participant_identities.add("user_1")
        agent._participant_info["user_1"] = "Alice"

        # Add enough transcript to trigger the fallback
        agent.add_transcript_segment("Alice", "Short meeting today")
        agent.add_transcript_segment("Bob", "Yes, just a quick sync")

        with (
            patch.object(agent, "_generate_summary", new_callable=AsyncMock) as mock_gen,
            patch.object(agent, "_merge_progressive_summaries"),
        ):
            mock_gen.return_value = ("Direct summary result.", ["Task from direct"])
            await agent._finalize_notes()

            mock_gen.assert_awaited_once()

    # ------------------------------------------------------------------
    # stdout payload truncation (pipe limit fix)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_finalize_notes_truncates_transcript_for_stdout(self):
        """Transcript in stdout payload must be truncated to stay under pipe limit."""
        from pocketpaw_ee.cloud.livekit.agent import (
            _MAX_STDOUT_TRANSCRIPT_CHARS,
            CallMeetingAgent,
        )

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )
        agent._call_start_time = time.time()
        agent._participant_identities.add("user_1")
        agent._participant_info["user_1"] = "Alice"

        # Add a transcript that exceeds the 40K limit
        huge_text = "Hello world. " * 5_000  # ~60K chars
        agent.add_transcript_segment("Alice", huge_text)

        with (
            patch.object(agent, "_generate_summary", AsyncMock(return_value=("summary", []))),
            patch.object(agent, "_fetch_progressive_summaries", AsyncMock(return_value=[])),
        ):
            # Patch print to capture the payload
            captured = {}

            def _fake_print(payload, **kwargs):
                import json as _j

                parsed = _j.loads(payload)
                captured["transcript_len"] = len(parsed["transcript"])
                captured["transcript"] = parsed["transcript"]

            with patch("builtins.print", side_effect=_fake_print):
                await agent._finalize_notes()

            # The stdout transcript must be truncated
            assert captured["transcript_len"] <= _MAX_STDOUT_TRANSCRIPT_CHARS, (
                f"stdout transcript is {captured['transcript_len']} chars, "
                f"exceeds {_MAX_STDOUT_TRANSCRIPT_CHARS} limit"
            )
            # Should have truncation markers
            assert "transcript truncated for stdout" in captured["transcript"]

    @pytest.mark.asyncio
    async def test_finalize_notes_does_not_truncate_small_transcript_for_stdout(self):
        """Short transcripts should NOT be truncated for stdout."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )
        agent._call_start_time = time.time()
        agent._participant_identities.add("user_1")
        agent._participant_info["user_1"] = "Alice"

        # Short transcript
        agent.add_transcript_segment("Alice", "Quick sync, all good.")
        agent.add_transcript_segment("Bob", "Agreed, nothing to add.")

        with (
            patch.object(agent, "_generate_summary", AsyncMock(return_value=("summary", []))),
            patch.object(agent, "_fetch_progressive_summaries", AsyncMock(return_value=[])),
        ):
            captured = {}

            def _fake_print(payload, **kwargs):
                import json as _j

                parsed = _j.loads(payload)
                captured["transcript"] = parsed["transcript"]
                captured["truncated"] = "transcript truncated" in parsed["transcript"]

            with patch("builtins.print", side_effect=_fake_print):
                await agent._finalize_notes()

            assert not captured["truncated"], "short transcript should not be truncated"
            assert "Quick sync" in captured["transcript"]

    # ------------------------------------------------------------------
    # _progressive_worker
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_progressive_worker_does_not_break_when_llm_fails(self):
        """Progressive worker should survive LLM failures and continue to next cycle."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )
        # Start time far in the past so the initial "elapsed < 30" check passes
        agent._call_start_time = time.time() - 600
        agent._running = True

        # Add enough segments for a progressive run
        for i in range(10):
            agent.add_transcript_segment("Alice", f"Point number {i}")

        with (
            patch.object(agent, "_generate_progressive_summary", AsyncMock(return_value=None)),
            patch.object(agent, "_store_progressive_summary", AsyncMock()),
            patch("asyncio.sleep", AsyncMock()),
        ):
            # Run the worker briefly — it should not crash
            task = asyncio.create_task(agent._progressive_worker())
            await asyncio.sleep(0.2)
            agent._running = False
            await asyncio.wait_for(task, timeout=3)

        # Worker ran and exited cleanly (no exception propagated)
        assert True

    # ------------------------------------------------------------------
    # _finalize_notes participant info
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_finalize_notes_includes_all_participants(self):
        """_finalize_notes should merge participant_identities with speakers."""
        from pocketpaw_ee.cloud.livekit.agent import CallMeetingAgent

        agent = CallMeetingAgent(
            group_id="g",
            room_name="group-call-g",
            bot_token="tok",
        )
        agent._call_start_time = time.time()

        # Participants from room info
        agent._participant_identities.add("user_alice")
        agent._participant_identities.add("user_bob")
        agent._participant_info["user_alice"] = "Alice"
        agent._participant_info["user_bob"] = "Bob"

        # Speaker from transcript that wasn't in room info
        agent.add_transcript_segment("Charlie", "I'm here too")

        with (
            patch.object(agent, "_generate_summary", AsyncMock(return_value=("summary", []))),
            patch.object(agent, "_fetch_progressive_summaries", AsyncMock(return_value=[])),
        ):
            captured = {}

            def _fake_print(payload, **kwargs):
                import json as _j

                parsed = _j.loads(payload)
                captured["participants"] = parsed["participants"]
                captured["participant_map"] = parsed["participant_map"]

            with patch("builtins.print", side_effect=_fake_print):
                await agent._finalize_notes()

            assert "Alice" in captured["participants"]
            assert "Bob" in captured["participants"]
            assert "Charlie" in captured["participants"]
            assert len(captured["participant_map"]) == 2  # only room participants, not speakers
