"""Tests for Telegram Channel Adapter — typing indicators and smart buffering.

python-telegram-bot is mocked since it's an optional dependency.

Created: 2026-03-06
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock telegram before importing adapter
mock_telegram = MagicMock()
mock_telegram.Update = MagicMock()
mock_telegram.constants = MagicMock()
mock_telegram.constants.ChatAction = MagicMock()
mock_telegram.constants.ChatAction.TYPING = "typing"
mock_telegram.ForceReply = MagicMock()
mock_telegram.error = MagicMock()
sys.modules["telegram"] = mock_telegram
sys.modules["telegram.constants"] = mock_telegram.constants
sys.modules["telegram.error"] = mock_telegram.error
sys.modules["telegram.ext"] = MagicMock()

from pocketpaw.bus.adapters.telegram_adapter import (  # noqa: E402
    _BUFFER_UPDATE_INTERVAL,
    _TYPING_REFRESH_INTERVAL,
    TelegramAdapter,
)
from pocketpaw.bus.events import Channel, OutboundMessage  # noqa: E402


@pytest.fixture
def adapter():
    return TelegramAdapter(token="test-token", allowed_user_id=12345)


@pytest.fixture
def adapter_with_app(adapter):
    """Adapter with mocked Telegram app."""
    mock_bot = AsyncMock()
    mock_bot.send_chat_action = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    mock_bot.edit_message_text = AsyncMock()

    adapter.app = MagicMock()
    adapter.app.bot = mock_bot
    adapter.app.updater.stop = AsyncMock()
    adapter.app.stop = AsyncMock()
    adapter.app.shutdown = AsyncMock()
    return adapter


class TestTelegramAdapterInit:
    def test_defaults(self):
        adapter = TelegramAdapter(token="test-token")
        assert adapter.token == "test-token"
        assert adapter.allowed_user_id is None
        assert adapter.channel == Channel.TELEGRAM
        assert adapter._typing_tasks == {}

    def test_custom_config(self):
        adapter = TelegramAdapter(token="tok", allowed_user_id=999)
        assert adapter.token == "tok"
        assert adapter.allowed_user_id == 999

    def test_constants_defined(self):
        """Verify the timing constants are defined."""
        assert _TYPING_REFRESH_INTERVAL == 4.0
        assert _BUFFER_UPDATE_INTERVAL == 1.5


class TestTypingIndicator:
    async def test_send_typing_indicator(self, adapter_with_app):
        """_send_typing_indicator sends ChatAction.TYPING."""
        await adapter_with_app._send_typing_indicator("12345")

        adapter_with_app.app.bot.send_chat_action.assert_called_once()
        call_kwargs = adapter_with_app.app.bot.send_chat_action.call_args
        assert call_kwargs[1]["chat_id"] == "12345"

    async def test_send_typing_indicator_with_topic(self, adapter_with_app):
        """Typing indicator uses real chat_id, not topic suffix."""
        await adapter_with_app._send_typing_indicator("-100123:topic:42")

        call_kwargs = adapter_with_app.app.bot.send_chat_action.call_args
        assert call_kwargs[1]["chat_id"] == "-100123"

    async def test_send_typing_indicator_no_app(self, adapter):
        """No error when app is not initialized."""
        adapter.app = None
        # Should not raise
        await adapter._send_typing_indicator("12345")

    async def test_send_typing_indicator_exception_caught(self, adapter_with_app):
        """Exceptions in send_chat_action are caught."""
        adapter_with_app.app.bot.send_chat_action.side_effect = Exception("API error")
        # Should not raise
        await adapter_with_app._send_typing_indicator("12345")

    async def test_start_typing_indicator_creates_task(self, adapter_with_app):
        """_start_typing_indicator creates a background task."""
        adapter_with_app._start_typing_indicator("12345")

        assert "12345" in adapter_with_app._typing_tasks
        task = adapter_with_app._typing_tasks["12345"]
        assert isinstance(task, asyncio.Task)

        # Cleanup
        task.cancel()

    async def test_start_typing_indicator_idempotent(self, adapter_with_app):
        """Starting typing indicator twice doesn't create duplicate tasks."""
        adapter_with_app._start_typing_indicator("12345")
        first_task = adapter_with_app._typing_tasks["12345"]

        adapter_with_app._start_typing_indicator("12345")
        second_task = adapter_with_app._typing_tasks["12345"]

        assert first_task is second_task

        # Cleanup
        first_task.cancel()

    async def test_stop_typing_indicator_cancels_task(self, adapter_with_app):
        """_stop_typing_indicator cancels the task."""
        adapter_with_app._start_typing_indicator("12345")
        task = adapter_with_app._typing_tasks["12345"]

        adapter_with_app._stop_typing_indicator("12345")
        await asyncio.sleep(0)  # Let event loop deliver the CancelledError

        assert "12345" not in adapter_with_app._typing_tasks
        assert task.cancelled() or task.done()

    def test_stop_typing_indicator_nonexistent(self, adapter_with_app):
        """Stopping non-existent typing indicator doesn't raise."""
        adapter_with_app._stop_typing_indicator("nonexistent")


class TestStreamBuffering:
    async def test_first_chunk_sends_placeholder(self, adapter_with_app):
        """First stream chunk sends placeholder message and starts typing."""
        chunk = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="Hello ",
            is_stream_chunk=True,
        )
        await adapter_with_app._handle_stream_chunk(chunk)

        # Placeholder message sent
        adapter_with_app.app.bot.send_message.assert_called_once()
        call_kwargs = adapter_with_app.app.bot.send_message.call_args[1]
        assert call_kwargs["text"] == "🧠 ..."

        # Buffer created
        assert "12345" in adapter_with_app._buffers
        assert adapter_with_app._buffers["12345"]["text"] == "Hello "

        # Typing indicator started
        assert "12345" in adapter_with_app._typing_tasks

        # Cleanup
        adapter_with_app._stop_typing_indicator("12345")

    async def test_subsequent_chunks_accumulate(self, adapter_with_app):
        """Subsequent chunks accumulate in buffer."""
        # Prime the buffer
        adapter_with_app._buffers["12345"] = {
            "message_id": 1,
            "text": "Hello ",
            "last_update": asyncio.get_event_loop().time(),
        }

        chunk = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="World!",
            is_stream_chunk=True,
        )
        await adapter_with_app._handle_stream_chunk(chunk)

        assert adapter_with_app._buffers["12345"]["text"] == "Hello World!"

    async def test_rate_limited_update(self, adapter_with_app):
        """Message is updated when rate limit interval passes."""
        # Prime buffer with old timestamp
        adapter_with_app._buffers["12345"] = {
            "message_id": 1,
            "text": "Hello ",
            "last_update": asyncio.get_event_loop().time() - 2.0,  # 2 seconds ago
        }

        chunk = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="World!",
            is_stream_chunk=True,
        )
        await adapter_with_app._handle_stream_chunk(chunk)

        # Message should be updated
        adapter_with_app.app.bot.edit_message_text.assert_called_once()

    async def test_no_update_within_rate_limit(self, adapter_with_app):
        """Message is not updated within rate limit interval."""
        # Prime buffer with recent timestamp
        adapter_with_app._buffers["12345"] = {
            "message_id": 1,
            "text": "Hello ",
            "last_update": asyncio.get_event_loop().time(),
        }

        chunk = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="World!",
            is_stream_chunk=True,
        )
        await adapter_with_app._handle_stream_chunk(chunk)

        # Message should NOT be updated
        adapter_with_app.app.bot.edit_message_text.assert_not_called()

    async def test_stream_end_flushes_buffer(self, adapter_with_app):
        """Stream end flushes buffer and stops typing."""
        # Prime the buffer
        adapter_with_app._buffers["12345"] = {
            "message_id": 1,
            "text": "Final text",
            "last_update": 0,
        }
        adapter_with_app._start_typing_indicator("12345")

        end_msg = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="",
            is_stream_end=True,
        )
        await adapter_with_app.send(end_msg)

        # Buffer flushed
        assert "12345" not in adapter_with_app._buffers

        # Typing stopped
        assert "12345" not in adapter_with_app._typing_tasks

        # Final message edit
        adapter_with_app.app.bot.edit_message_text.assert_called()

    async def test_flush_stream_buffer_formats_markdown(self, adapter_with_app):
        """Flush converts markdown for Telegram."""
        adapter_with_app._buffers["12345"] = {
            "message_id": 1,
            "text": "**bold** text",
            "last_update": 0,
        }

        await adapter_with_app._flush_stream_buffer("12345")

        assert "12345" not in adapter_with_app._buffers
        adapter_with_app.app.bot.edit_message_text.assert_called_once()


class TestStreamingLifecycle:
    async def test_full_streaming_cycle(self, adapter_with_app):
        """Test complete streaming cycle: chunks -> end."""
        # First chunk
        chunk1 = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="Hello ",
            is_stream_chunk=True,
        )
        await adapter_with_app.send(chunk1)

        assert "12345" in adapter_with_app._buffers
        assert "12345" in adapter_with_app._typing_tasks

        # Second chunk
        chunk2 = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="World!",
            is_stream_chunk=True,
        )
        await adapter_with_app.send(chunk2)

        assert adapter_with_app._buffers["12345"]["text"] == "Hello World!"

        # Stream end
        end = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="",
            is_stream_end=True,
        )
        await adapter_with_app.send(end)

        # Everything cleaned up
        assert "12345" not in adapter_with_app._buffers
        assert "12345" not in adapter_with_app._typing_tasks


class TestOnStop:
    async def test_stop_cancels_all_typing_tasks(self, adapter_with_app):
        """_on_stop cancels all typing indicator tasks."""
        # Start multiple typing indicators
        adapter_with_app._start_typing_indicator("12345")
        adapter_with_app._start_typing_indicator("67890")

        task1 = adapter_with_app._typing_tasks["12345"]
        task2 = adapter_with_app._typing_tasks["67890"]

        await adapter_with_app._on_stop()
        await asyncio.sleep(0)  # Let event loop deliver the CancelledErrors

        assert len(adapter_with_app._typing_tasks) == 0
        assert task1.cancelled() or task1.done()
        assert task2.cancelled() or task2.done()


class TestMessageUpdate:
    async def test_update_message_empty_text_skipped(self, adapter_with_app):
        """Empty text is not sent to Telegram."""
        await adapter_with_app._update_message("12345", 1, "   ")

        adapter_with_app.app.bot.edit_message_text.assert_not_called()

    async def test_update_message_exception_caught(self, adapter_with_app):
        """Exceptions in edit_message_text are caught."""
        adapter_with_app.app.bot.edit_message_text.side_effect = Exception("API error")

        # Should not raise
        await adapter_with_app._update_message("12345", 1, "test")

    async def test_update_message_with_topic(self, adapter_with_app):
        """Update message parses topic correctly."""
        await adapter_with_app._update_message("-100123:topic:42", 1, "test")

        call_kwargs = adapter_with_app.app.bot.edit_message_text.call_args[1]
        assert call_kwargs["chat_id"] == "-100123"


class TestVoiceMediaRouting:
    def test_is_voice_media_false_without_metadata(self):
        assert TelegramAdapter._is_voice_media("/tmp/reply.mp3", None) is False

    def test_is_voice_media_true_for_is_voice_flag(self):
        assert TelegramAdapter._is_voice_media("/tmp/reply.mp3", {"is_voice": True}) is True

    def test_is_voice_media_true_for_voice_media_paths_match(self):
        path = "/tmp/reply.mp3"
        metadata = {"voice_media_paths": [path]}
        assert TelegramAdapter._is_voice_media(path, metadata) is True

    async def test_send_media_file_voice_falls_back_to_audio(self, adapter, tmp_path):
        media_file = tmp_path / "reply.mp3"
        media_file.write_bytes(b"fake-audio")

        mock_bot = AsyncMock()
        mock_bot.send_voice = AsyncMock(side_effect=Exception("voice send failed"))
        mock_bot.send_audio = AsyncMock()

        adapter.app = MagicMock()
        adapter.app.bot = mock_bot

        chat_id = "12345"
        file_path = str(media_file)
        hint_key = (chat_id, file_path)
        adapter._voice_media_hints[hint_key] = True
        adapter._voice_media_hints_ts[hint_key] = asyncio.get_running_loop().time()

        with patch("pocketpaw.bus.adapters.guess_media_type", return_value="audio"):
            await adapter._send_media_file(chat_id, file_path)

        mock_bot.send_voice.assert_called_once()
        mock_bot.send_audio.assert_called_once()
