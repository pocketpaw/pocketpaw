"""Tests for the LiveKit call service.

Added TestSelectiveSubscribe: covers the call bot's audio-only selective
subscribe (Bug A) — auto_subscribe=False on connect and the pure
_should_subscribe() decision (mic audio in, video / screenshare out).

Added TestSpawnRace: covers the duplicate call-bot spawn race — concurrent
create_room() for the same group must spawn exactly one agent (per-group
asyncio.Lock), and create_room() must skip the spawn when a "call-bot" is
already present in the LiveKit room (cross-replica check via _call_bot_in_room).
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

    @pytest.mark.asyncio
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key")
    @patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret")
    async def test_create_room_skips_spawn_when_call_bot_already_present(
        self, mock_lk_existing_room
    ):
        """If a "call-bot" is already in the LiveKit room (e.g. spawned by
        another replica), create_room() must NOT spawn a second one."""
        from pocketpaw_ee.cloud.livekit.service import create_room

        callbot = MagicMock()
        callbot.identity = "call-bot"
        parts_resp = MagicMock()
        parts_resp.participants = [callbot]
        mock_lk_existing_room.list_participants = AsyncMock(return_value=parts_resp)

        with patch(
            "pocketpaw_ee.cloud.livekit.service._spawn_agent_process",
            new_callable=AsyncMock,
        ) as mock_spawn:
            await create_room("g", "ws", "u")

        mock_spawn.assert_not_called()
        assert "g" not in _active_agents
