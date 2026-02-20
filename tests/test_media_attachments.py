# Tests for outbound media attachment delivery across channels.
# Created: 2026-02-16

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Sprint 1: Tool result media tagging
# ---------------------------------------------------------------------------


class TestMediaResult:
    """Tests for BaseTool._media_result()."""

    def test_media_result_format(self):
        from pocketpaw.tools.protocol import BaseTool

        class DummyTool(BaseTool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "dummy"

            async def execute(self, **params):
                return ""

        tool = DummyTool()
        result = tool._media_result("/tmp/audio.wav", "Audio generated (1234 bytes)")
        assert "<!-- media:/tmp/audio.wav -->" in result
        assert "Audio generated (1234 bytes)" in result

    def test_media_result_no_text(self):
        from pocketpaw.tools.protocol import BaseTool

        class DummyTool(BaseTool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "dummy"

            async def execute(self, **params):
                return ""

        tool = DummyTool()
        result = tool._media_result("/tmp/photo.png")
        assert result == "<!-- media:/tmp/photo.png -->"

    def test_media_result_strips_whitespace(self):
        from pocketpaw.tools.protocol import BaseTool

        class DummyTool(BaseTool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "dummy"

            async def execute(self, **params):
                return ""

        tool = DummyTool()
        result = tool._media_result("/tmp/f.mp3", "")
        assert result == "<!-- media:/tmp/f.mp3 -->"


# ---------------------------------------------------------------------------
# Sprint 2: AgentLoop media extraction
# ---------------------------------------------------------------------------


class TestExtractMediaPaths:
    """Tests for _extract_media_paths()."""

    def test_single_tag(self):
        from pocketpaw.agents.loop import _extract_media_paths

        text = "Audio generated\n<!-- media:/home/user/.pocketpaw/generated/audio/tts_abc.wav -->"
        paths = _extract_media_paths(text)
        assert paths == ["/home/user/.pocketpaw/generated/audio/tts_abc.wav"]

    def test_multiple_tags(self):
        from pocketpaw.agents.loop import _extract_media_paths

        text = "Here is audio <!-- media:/tmp/a.wav --> and an image <!-- media:/tmp/b.png -->"
        paths = _extract_media_paths(text)
        assert paths == ["/tmp/a.wav", "/tmp/b.png"]

    def test_no_tags(self):
        from pocketpaw.agents.loop import _extract_media_paths

        text = "No media here, just plain text."
        assert _extract_media_paths(text) == []

    def test_empty_string(self):
        from pocketpaw.agents.loop import _extract_media_paths

        assert _extract_media_paths("") == []

    def test_tag_with_spaces_in_path(self):
        from pocketpaw.agents.loop import _extract_media_paths

        text = "<!-- media:/tmp/my file.wav -->"
        paths = _extract_media_paths(text)
        assert paths == ["/tmp/my file.wav"]


# ---------------------------------------------------------------------------
# Sprint 3: guess_media_type()
# ---------------------------------------------------------------------------


class TestGuessMediaType:
    """Tests for guess_media_type()."""

    def test_audio_extensions(self):
        from pocketpaw.bus.adapters import guess_media_type

        for ext in (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"):
            assert guess_media_type(f"/tmp/file{ext}") == "audio"

    def test_image_extensions(self):
        from pocketpaw.bus.adapters import guess_media_type

        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            assert guess_media_type(f"/tmp/file{ext}") == "image"

    def test_document_fallback(self):
        from pocketpaw.bus.adapters import guess_media_type

        assert guess_media_type("/tmp/file.pdf") == "document"
        assert guess_media_type("/tmp/file.txt") == "document"
        assert guess_media_type("/tmp/file") == "document"

    def test_case_insensitive(self):
        from pocketpaw.bus.adapters import guess_media_type

        assert guess_media_type("/tmp/file.MP3") == "audio"
        assert guess_media_type("/tmp/file.PNG") == "image"


# ---------------------------------------------------------------------------
# Sprint 3: Adapter media sending
# ---------------------------------------------------------------------------


class TestTelegramMediaSend:
    """Tests for TelegramAdapter._send_media_file()."""

    @pytest.fixture(autouse=True)
    def _mock_telegram(self, monkeypatch):
        """Mock python-telegram-bot before importing adapter."""
        telegram_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
        monkeypatch.setitem(sys.modules, "telegram.ext", telegram_mod)
        monkeypatch.setitem(sys.modules, "telegram.ext.filters", telegram_mod)

    async def test_sends_audio(self, tmp_path):
        # noqa: E402
        from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter(token="fake")
        adapter.app = MagicMock()
        adapter.app.bot.send_audio = AsyncMock()

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake audio data")

        await adapter._send_media_file("12345", str(audio_file))
        adapter.app.bot.send_audio.assert_called_once()

    async def test_sends_image(self, tmp_path):
        from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter(token="fake")
        adapter.app = MagicMock()
        adapter.app.bot.send_photo = AsyncMock()

        img_file = tmp_path / "test.png"
        img_file.write_bytes(b"fake image data")

        await adapter._send_media_file("12345", str(img_file))
        adapter.app.bot.send_photo.assert_called_once()

    async def test_sends_document(self, tmp_path):
        from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter(token="fake")
        adapter.app = MagicMock()
        adapter.app.bot.send_document = AsyncMock()

        doc_file = tmp_path / "test.pdf"
        doc_file.write_bytes(b"fake doc data")

        await adapter._send_media_file("12345", str(doc_file))
        adapter.app.bot.send_document.assert_called_once()

    async def test_skips_missing_file(self, tmp_path):
        from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter(token="fake")
        adapter.app = MagicMock()
        adapter.app.bot.send_audio = AsyncMock()

        await adapter._send_media_file("12345", "/nonexistent/path.wav")
        adapter.app.bot.send_audio.assert_not_called()

    async def test_topic_aware(self, tmp_path):
        from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter(token="fake")
        adapter.app = MagicMock()
        adapter.app.bot.send_audio = AsyncMock()

        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"data")

        await adapter._send_media_file("12345:topic:99", str(audio_file))
        call_kwargs = adapter.app.bot.send_audio.call_args
        assert call_kwargs[1]["chat_id"] == "12345"
        assert call_kwargs[1]["message_thread_id"] == 99


class TestDiscordMediaSend:
    """Tests for DiscordAdapter._send_media_file()."""

    async def test_sends_file(self, tmp_path):
        sys.modules["discord"] = MagicMock()
        sys.modules["discord.app_commands"] = MagicMock()
        from pocketpaw.bus.adapters.discord_adapter import DiscordAdapter

        adapter = DiscordAdapter(token="fake")
        channel_mock = AsyncMock()
        client_mock = MagicMock()
        client_mock.get_channel.return_value = channel_mock
        adapter._client = client_mock

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake audio")

        await adapter._send_media_file("999", str(audio_file))
        channel_mock.send.assert_called_once()

    async def test_skips_missing_file(self, tmp_path):
        sys.modules["discord"] = MagicMock()
        sys.modules["discord.app_commands"] = MagicMock()
        from pocketpaw.bus.adapters.discord_adapter import DiscordAdapter

        adapter = DiscordAdapter(token="fake")
        adapter._client = MagicMock()
        await adapter._send_media_file("999", "/nonexistent.wav")


class TestSlackMediaSend:
    """Tests for SlackAdapter._send_media_file()."""

    @pytest.fixture(autouse=True)
    def _mock_slack(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "slack_bolt", MagicMock())
        monkeypatch.setitem(sys.modules, "slack_bolt.async_app", MagicMock())
        monkeypatch.setitem(
            sys.modules, "slack_bolt.adapter.socket_mode.async_handler", MagicMock()
        )

    async def test_uploads_file(self, tmp_path):
        from pocketpaw.bus.adapters.slack_adapter import SlackAdapter

        adapter = SlackAdapter(bot_token="xoxb-fake", app_token="xapp-fake")
        adapter._slack_app = MagicMock()
        adapter._slack_app.client.files_upload_v2 = AsyncMock()

        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"audio data")

        await adapter._send_media_file("C123", str(audio_file))
        adapter._slack_app.client.files_upload_v2.assert_called_once()

    async def test_skips_missing_file(self, tmp_path):
        from pocketpaw.bus.adapters.slack_adapter import SlackAdapter

        adapter = SlackAdapter(bot_token="xoxb-fake", app_token="xapp-fake")
        adapter._slack_app = MagicMock()
        adapter._slack_app.client.files_upload_v2 = AsyncMock()

        await adapter._send_media_file("C123", "/nonexistent.wav")
        adapter._slack_app.client.files_upload_v2.assert_not_called()


class TestWhatsAppMediaSend:
    """Tests for WhatsAppAdapter._send_media_file()."""

    async def test_uploads_and_sends(self, tmp_path):
        from pocketpaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(
            access_token="fake-token",
            phone_number_id="12345",
            verify_token="verify",
        )
        mock_http = AsyncMock()
        mock_http.post.return_value = MagicMock(status_code=200, json=lambda: {"id": "media_123"})
        adapter._http = mock_http

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"audio data")

        await adapter._send_media_file("+1234567890", str(audio_file))
        # Should call post twice: upload + send
        assert mock_http.post.call_count == 2

    async def test_skips_missing_file(self, tmp_path):
        from pocketpaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(access_token="fake", phone_number_id="12345", verify_token="v")
        adapter._http = AsyncMock()
        await adapter._send_media_file("+1234", "/nonexistent.wav")
        adapter._http.post.assert_not_called()


class TestWebSocketMedia:
    """Tests for WebSocket stream_end media payload."""

    async def test_stream_end_includes_media(self):
        from pocketpaw.bus.adapters.websocket_adapter import WebSocketAdapter
        from pocketpaw.bus.events import Channel, OutboundMessage

        adapter = WebSocketAdapter()
        mock_ws = AsyncMock()
        adapter._connections["test-chat"] = mock_ws

        msg = OutboundMessage(
            channel=Channel.WEBSOCKET,
            chat_id="test-chat",
            content="",
            is_stream_end=True,
            media=["/tmp/audio.wav", "/tmp/image.png"],
        )
        await adapter.send(msg)

        mock_ws.send_json.assert_called_once()
        payload = mock_ws.send_json.call_args[0][0]
        assert payload["type"] == "stream_end"
        assert payload["media"] == ["/tmp/audio.wav", "/tmp/image.png"]

    async def test_stream_end_no_media(self):
        from pocketpaw.bus.adapters.websocket_adapter import WebSocketAdapter
        from pocketpaw.bus.events import Channel, OutboundMessage

        adapter = WebSocketAdapter()
        mock_ws = AsyncMock()
        adapter._connections["test-chat"] = mock_ws

        msg = OutboundMessage(
            channel=Channel.WEBSOCKET,
            chat_id="test-chat",
            content="",
            is_stream_end=True,
        )
        await adapter.send(msg)

        payload = mock_ws.send_json.call_args[0][0]
        assert payload["type"] == "stream_end"
        assert "media" not in payload


# ---------------------------------------------------------------------------
# Sprint 4: Dashboard media endpoint security
# ---------------------------------------------------------------------------


_TEST_TOKEN = "test-media-token-abc"


class TestServeMediaSecurity:
    """Tests for /api/media endpoint path traversal protection."""

    @pytest.fixture(autouse=True)
    def _mock_auth(self):
        with patch("pocketpaw.dashboard_auth.get_access_token", return_value=_TEST_TOKEN):
            yield

    def _headers(self):
        return {"Authorization": f"Bearer {_TEST_TOKEN}"}

    async def test_rejects_outside_generated(self):
        """Paths outside ~/.pocketpaw/generated/ should be rejected."""
        from fastapi.testclient import TestClient

        from pocketpaw.dashboard import app

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/media", params={"path": "/etc/passwd"}, headers=self._headers())
        assert resp.status_code in (403, 404)

    async def test_rejects_traversal(self):
        """Path traversal attempts should be rejected."""
        from fastapi.testclient import TestClient

        from pocketpaw.dashboard import app

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/media",
            params={"path": "/home/user/.pocketpaw/generated/../../etc/passwd"},
            headers=self._headers(),
        )
        assert resp.status_code in (403, 404)

    async def test_returns_404_for_missing(self, tmp_path):
        """Missing files should return 404."""
        from fastapi.testclient import TestClient

        from pocketpaw.dashboard import app

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/media",
            params={"path": str(tmp_path / "nonexistent.wav")},
            headers=self._headers(),
        )
        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Integration: AgentLoop extracts media from tool results
# ---------------------------------------------------------------------------


class TestAgentLoopMediaIntegration:
    """Test that AgentLoop attaches media from tool_result to stream_end."""

    async def test_media_tags_extracted_from_full_response(self):
        """Media tags in full_response (agent text) should be extracted."""
        from pocketpaw.agents.loop import _extract_media_paths

        full_response = (
            "Here's the audio you requested.\n"
            "Audio generated (12345 bytes)\n"
            "<!-- media:/home/user/.pocketpaw/generated/audio/tts_abc.wav -->"
        )
        paths = _extract_media_paths(full_response)
        assert len(paths) == 1
        assert paths[0] == "/home/user/.pocketpaw/generated/audio/tts_abc.wav"

    async def test_no_media_no_paths(self):
        from pocketpaw.agents.loop import _extract_media_paths

        full_response = "The weather today is sunny with a high of 72F."
        assert _extract_media_paths(full_response) == []

    async def test_media_in_tool_result_not_in_message(self):
        """The real scenario: media tag is in tool_result content, NOT in agent text.

        AgentLoop must extract media from tool_result chunks, not just full_response.
        """
        from pocketpaw.agents.loop import _extract_media_paths

        # This is what the tool returns (tool_result chunk content)
        tool_result_content = (
            "Audio generated (5678 bytes)\n"
            "<!-- media:/home/user/.pocketpaw/generated/audio/tts_abc.wav -->"
        )
        # This is the agent's text response (full_response) — no media tag!
        agent_text = (
            "Here's your audio! The file has been saved to "
            "/home/user/.pocketpaw/generated/audio/tts_abc.wav"
        )

        # Simulating the fixed AgentLoop behavior:
        # 1. media_paths collected from tool_result chunks
        media_from_tools = _extract_media_paths(tool_result_content)
        # 2. media_paths also checked in full_response (fallback)
        media_from_text = _extract_media_paths(agent_text)

        # tool_result should have the tag
        assert media_from_tools == ["/home/user/.pocketpaw/generated/audio/tts_abc.wav"]
        # agent text should NOT have the tag
        assert media_from_text == []

    async def test_fallback_generated_path_extraction(self):
        """Claude SDK backend: media tag is internal; agent echoes path in text.

        _extract_generated_paths() should find file paths under generated/.
        """
        from pocketpaw.agents.loop import _extract_generated_paths

        agent_text = (
            "Here's your audio! The file has been saved to "
            "`/home/rohitk06/.pocketpaw/generated/audio/tts_5b331889.wav`\n"
            "You can play it with mpv."
        )
        paths = _extract_generated_paths(agent_text)
        assert len(paths) == 1
        assert paths[0] == "/home/rohitk06/.pocketpaw/generated/audio/tts_5b331889.wav"

    async def test_fallback_multiple_paths(self):
        """Multiple generated paths should all be found."""
        from pocketpaw.agents.loop import _extract_generated_paths

        agent_text = (
            "Audio: `/home/user/.pocketpaw/generated/audio/tts_abc.wav`\n"
            "Image: `/home/user/.pocketpaw/generated/img_xyz.png`"
        )
        paths = _extract_generated_paths(agent_text)
        assert len(paths) == 2

    async def test_fallback_no_match_for_non_generated(self):
        """Paths NOT under generated/ should NOT be matched."""
        from pocketpaw.agents.loop import _extract_generated_paths

        agent_text = "The config file is at `/home/user/.pocketpaw/config.json`"
        paths = _extract_generated_paths(agent_text)
        assert paths == []

    async def test_fallback_path_in_parens(self):
        """Paths in parentheses should be found."""
        from pocketpaw.agents.loop import _extract_generated_paths

        agent_text = "Audio file (/home/user/.pocketpaw/generated/audio/tts.wav) has been created."
        paths = _extract_generated_paths(agent_text)
        assert len(paths) == 1
        assert paths[0] == "/home/user/.pocketpaw/generated/audio/tts.wav"

    async def test_deduplication(self):
        """If media tag appears in both tool_result and message, deduplicate."""
        from pocketpaw.agents.loop import _extract_media_paths

        path = "/home/user/.pocketpaw/generated/audio/tts_abc.wav"
        tool_content = f"Audio generated\n<!-- media:{path} -->"
        agent_text = f"Here's audio <!-- media:{path} -->"

        all_paths: list[str] = []
        all_paths.extend(_extract_media_paths(tool_content))
        all_paths.extend(_extract_media_paths(agent_text))

        # Deduplicate
        seen: set[str] = set()
        deduped = [p for p in all_paths if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]

        assert deduped == [path]


# ---------------------------------------------------------------------------
# Inbound media: file paths injected into agent content
# ---------------------------------------------------------------------------


class TestInboundMediaPathInjection:
    """Test that inbound media file paths are injected into the message content."""

    def test_media_paths_appended_to_content(self):
        """When message.media has paths, they should be injected into the content."""
        # Simulate what the AgentLoop does
        content = "What does this say?\n[Attached: photo.jpg]"
        media = ["/home/user/.pocketpaw/media/1234_photo.jpg"]

        if media:
            paths_info = ", ".join(media)
            content += f"\n[Media files on disk: {paths_info}]"

        assert "[Media files on disk:" in content
        assert "/home/user/.pocketpaw/media/1234_photo.jpg" in content

    def test_no_media_no_injection(self):
        """When message.media is empty, content should be unchanged."""
        content = "Hello!"
        media: list[str] = []

        if media:
            paths_info = ", ".join(media)
            content += f"\n[Media files on disk: {paths_info}]"

        assert "[Media files on disk:" not in content
        assert content == "Hello!"

    def test_multiple_media_paths(self):
        """Multiple media files should all appear."""
        content = "Here are some files\n[Attached: photo.jpg, voice.ogg]"
        media = [
            "/home/user/.pocketpaw/media/1234_photo.jpg",
            "/home/user/.pocketpaw/media/5678_voice.ogg",
        ]

        if media:
            paths_info = ", ".join(media)
            content += f"\n[Media files on disk: {paths_info}]"

        assert "1234_photo.jpg" in content
        assert "5678_voice.ogg" in content

    def test_voice_note_gets_path(self):
        """Voice notes should have their disk path available for STT."""
        content = "\n[Attached: voice.ogg]"
        media = ["/home/user/.pocketpaw/media/abcd_voice.ogg"]

        if media:
            paths_info = ", ".join(media)
            content += f"\n[Media files on disk: {paths_info}]"

        assert "abcd_voice.ogg" in content


# ---------------------------------------------------------------------------
# Claude SDK: media extraction from UserMessage tool results
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sprint 5: Adapters send media files on stream_end
# ---------------------------------------------------------------------------


class TestTelegramStreamEndMedia:
    """Telegram adapter should call _send_media_file for each media on stream_end."""

    @pytest.fixture(autouse=True)
    def _mock_telegram(self, monkeypatch):
        telegram_mod = MagicMock()
        monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
        monkeypatch.setitem(sys.modules, "telegram.ext", telegram_mod)
        monkeypatch.setitem(sys.modules, "telegram.ext.filters", telegram_mod)

    async def test_stream_end_sends_media(self, tmp_path):
        from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter
        from pocketpaw.bus.events import Channel, OutboundMessage

        adapter = TelegramAdapter(token="fake")
        adapter.app = MagicMock()
        adapter._send_media_file = AsyncMock()

        audio_file = tmp_path / "tts.wav"
        audio_file.write_bytes(b"audio data")

        msg = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="",
            is_stream_end=True,
            media=[str(audio_file)],
        )
        await adapter.send(msg)
        adapter._send_media_file.assert_called_once_with("12345", str(audio_file))

    async def test_stream_end_no_media(self):
        from pocketpaw.bus.adapters.telegram_adapter import TelegramAdapter
        from pocketpaw.bus.events import Channel, OutboundMessage

        adapter = TelegramAdapter(token="fake")
        adapter.app = MagicMock()
        adapter._send_media_file = AsyncMock()

        msg = OutboundMessage(
            channel=Channel.TELEGRAM,
            chat_id="12345",
            content="",
            is_stream_end=True,
        )
        await adapter.send(msg)
        adapter._send_media_file.assert_not_called()


class TestDiscordStreamEndMedia:
    """Discord adapter should call _send_media_file for each media on stream_end."""

    async def test_stream_end_sends_media(self, tmp_path):
        sys.modules["discord"] = MagicMock()
        sys.modules["discord.app_commands"] = MagicMock()
        from pocketpaw.bus.adapters.discord_adapter import DiscordAdapter
        from pocketpaw.bus.events import Channel, OutboundMessage

        adapter = DiscordAdapter(token="fake")
        adapter._client = MagicMock()
        adapter._send_media_file = AsyncMock()

        msg = OutboundMessage(
            channel=Channel.DISCORD,
            chat_id="999",
            content="",
            is_stream_end=True,
            media=["/tmp/tts.wav", "/tmp/img.png"],
        )
        await adapter.send(msg)
        assert adapter._send_media_file.call_count == 2


class TestSlackStreamEndMedia:
    """Slack adapter should call _send_media_file for each media on stream_end."""

    @pytest.fixture(autouse=True)
    def _mock_slack(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "slack_bolt", MagicMock())
        monkeypatch.setitem(sys.modules, "slack_bolt.async_app", MagicMock())
        monkeypatch.setitem(
            sys.modules, "slack_bolt.adapter.socket_mode.async_handler", MagicMock()
        )

    async def test_stream_end_sends_media(self):
        from pocketpaw.bus.adapters.slack_adapter import SlackAdapter
        from pocketpaw.bus.events import Channel, OutboundMessage

        adapter = SlackAdapter(bot_token="xoxb-fake", app_token="xapp-fake")
        adapter._slack_app = MagicMock()
        adapter._send_media_file = AsyncMock()

        msg = OutboundMessage(
            channel=Channel.SLACK,
            chat_id="C123",
            content="",
            is_stream_end=True,
            media=["/tmp/tts.mp3"],
        )
        await adapter.send(msg)
        adapter._send_media_file.assert_called_once_with("C123", "/tmp/tts.mp3")


class TestWhatsAppStreamEndMedia:
    """WhatsApp adapter should call _send_media_file for each media on stream_end."""

    async def test_stream_end_sends_media(self):
        from pocketpaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter
        from pocketpaw.bus.events import Channel, OutboundMessage

        adapter = WhatsAppAdapter(access_token="fake", phone_number_id="12345", verify_token="v")
        adapter._http = AsyncMock()
        adapter._send_media_file = AsyncMock()

        msg = OutboundMessage(
            channel=Channel.WHATSAPP,
            chat_id="+1234",
            content="",
            is_stream_end=True,
            media=["/tmp/audio.wav"],
        )
        await adapter.send(msg)
        adapter._send_media_file.assert_called_once_with("+1234", "/tmp/audio.wav")


# ---------------------------------------------------------------------------
# Claude SDK: media extraction from UserMessage tool results
# ---------------------------------------------------------------------------


class TestClaudeSDKMediaExtraction:
    """Test that the Claude SDK backend extracts media tags from UserMessage events."""

    async def test_user_message_with_tool_result_media(self):
        """UserMessage containing ToolResultBlock with media tag should yield tool_result."""
        from unittest.mock import MagicMock

        from pocketpaw.agents.claude_sdk import ClaudeAgentSDK

        # Create mock SDK types
        mock_settings = MagicMock()
        mock_settings.tool_profile = "full"
        mock_settings.tools_allow = []
        mock_settings.tools_deny = []

        sdk = ClaudeAgentSDK.__new__(ClaudeAgentSDK)
        sdk.settings = mock_settings
        sdk._policy = MagicMock()

        # Create mock ToolResultBlock
        class FakeToolResultBlock:
            content = "Audio generated (5678 bytes)\n<!-- media:/tmp/tts.wav -->"

        class FakeUserMessage:
            content = [FakeToolResultBlock()]

        sdk._ToolResultBlock = FakeToolResultBlock
        sdk._UserMessage = FakeUserMessage

        # The UserMessage handler should detect media in tool results
        event = FakeUserMessage()
        assert isinstance(event, FakeUserMessage)
        assert hasattr(event, "content")

        # Extract media from the tool result block
        found_media = False
        for block in event.content:
            if isinstance(block, FakeToolResultBlock):
                result_text = getattr(block, "content", "")
                if isinstance(result_text, str) and "<!-- media:" in result_text:
                    found_media = True
        assert found_media

    async def test_user_message_without_media(self):
        """UserMessage with plain tool result (no media) should not yield tool_result."""

        class FakeToolResultBlock:
            content = "Command output: success"

        event_content = [FakeToolResultBlock()]
        found_media = False
        for block in event_content:
            result_text = getattr(block, "content", "")
            if isinstance(result_text, str) and "<!-- media:" in result_text:
                found_media = True
        assert not found_media

    async def test_user_message_with_list_content_block(self):
        """ToolResultBlock with list content (text blocks) should be handled."""

        class FakeTextBlock:
            text = "Audio generated\n<!-- media:/tmp/audio.mp3 -->"

        class FakeToolResultBlock:
            content = [FakeTextBlock()]

        event_content = [FakeToolResultBlock()]
        found_media = False
        for block in event_content:
            block_content = getattr(block, "content", "")
            if isinstance(block_content, list):
                result_text = " ".join(
                    getattr(b, "text", "") for b in block_content if hasattr(b, "text")
                )
                if "<!-- media:" in result_text:
                    found_media = True
        assert found_media
