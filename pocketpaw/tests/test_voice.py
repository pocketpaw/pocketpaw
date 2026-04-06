# Tests for tools/builtin/voice.py
# Created: 2026-02-07

from unittest.mock import AsyncMock, MagicMock, patch

from pocketpaw.tools.builtin.voice import TextToSpeechTool, _get_audio_dir

# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class TestToolDefinition:
    def test_name(self):
        tool = TextToSpeechTool()
        assert tool.name == "text_to_speech"

    def test_trust_level(self):
        tool = TextToSpeechTool()
        assert tool.trust_level == "standard"

    def test_parameters(self):
        tool = TextToSpeechTool()
        assert "text" in tool.parameters["properties"]
        assert "voice" in tool.parameters["properties"]
        assert "text" in tool.parameters["required"]


# ---------------------------------------------------------------------------
# Execution — error paths (no API keys)
# ---------------------------------------------------------------------------


async def test_openai_no_key():
    tool = TextToSpeechTool()
    mock_settings = MagicMock()
    mock_settings.tts_provider = "openai"
    mock_settings.tts_voice = "alloy"
    mock_settings.openai_api_key = None

    with patch("pocketpaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        result = await tool.execute(text="Hello world")
        assert "Error" in result
        assert "OpenAI" in result


async def test_elevenlabs_no_key():
    tool = TextToSpeechTool()
    mock_settings = MagicMock()
    mock_settings.tts_provider = "elevenlabs"
    mock_settings.tts_voice = "test-voice-id"
    mock_settings.elevenlabs_api_key = None

    with patch("pocketpaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        result = await tool.execute(text="Hello world")
        assert "Error" in result
        assert "ElevenLabs" in result


async def test_unknown_provider():
    tool = TextToSpeechTool()
    mock_settings = MagicMock()
    mock_settings.tts_provider = "unknown"
    mock_settings.tts_voice = "x"

    with patch("pocketpaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        result = await tool.execute(text="Hello")
        assert "Error" in result
        assert "Unknown TTS provider" in result


# ---------------------------------------------------------------------------
# Audio directory
# ---------------------------------------------------------------------------


def test_get_audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("pocketpaw.tools.builtin.voice.get_config_dir", lambda: tmp_path)
    d = _get_audio_dir()
    assert d.exists()
    assert d == tmp_path / "generated" / "audio"


# ---------------------------------------------------------------------------
# ElevenLabs TTS provider tests
# ---------------------------------------------------------------------------


async def test_elevenlabs_tts_success(tmp_path):
    """Test ElevenLabs TTS provider generates speech successfully."""
    tool = TextToSpeechTool()

    mock_settings = MagicMock()
    mock_settings.tts_provider = "elevenlabs"
    mock_settings.tts_voice = "test-voice-id"
    mock_settings.elevenlabs_api_key = "test-elevenlabs-key"

    mock_resp = MagicMock()
    mock_resp.content = b"fake_audio_data_elevenlabs"
    mock_resp.raise_for_status = MagicMock()

    with patch("pocketpaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("pocketpaw.tools.builtin.voice._get_audio_dir", return_value=tmp_path):
                result = await tool.execute(text="Hello ElevenLabs")

    assert "<!-- media:" in result
    assert tmp_path.name in result or "tts_" in result

    # Verify correct API endpoint was called
    call_args = mock_client.post.call_args
    assert "elevenlabs.io" in call_args[0][0]
    assert "text-to-speech" in call_args[0][0]
    assert call_args[1]["headers"]["xi-api-key"] == "test-elevenlabs-key"
    assert call_args[1]["json"]["text"] == "Hello ElevenLabs"
    assert call_args[1]["json"]["model_id"] == "eleven_multilingual_v2"


async def test_elevenlabs_tts_api_error(tmp_path):
    """Test ElevenLabs TTS handles API errors gracefully."""
    import httpx as httpx_mod

    tool = TextToSpeechTool()

    mock_settings = MagicMock()
    mock_settings.tts_provider = "elevenlabs"
    mock_settings.tts_voice = "test-voice-id"
    mock_settings.elevenlabs_api_key = "test-key"

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.request = MagicMock()

    with patch("pocketpaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                side_effect=httpx_mod.HTTPStatusError(
                    "server error", request=mock_resp.request, response=mock_resp
                )
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(text="Hello")

    assert "Error" in result
    assert "500" in result


async def test_synthesize_speech_helper(tmp_path):
    """Test the synthesize_speech standalone helper function."""
    from pocketpaw.tools.builtin.voice import synthesize_speech

    mock_settings = MagicMock()
    mock_settings.tts_provider = "elevenlabs"
    mock_settings.tts_voice = "test-voice-id"
    mock_settings.elevenlabs_api_key = "test-key"

    mock_resp = MagicMock()
    mock_resp.content = b"audio_bytes"
    mock_resp.raise_for_status = MagicMock()

    with patch("pocketpaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("pocketpaw.tools.builtin.voice._get_audio_dir", return_value=tmp_path):
                result = await synthesize_speech("Test text")

    assert result is not None
    assert isinstance(result, str)
    assert tmp_path.name in result or "tts_" in result


async def test_synthesize_speech_returns_none_on_error(tmp_path):
    """Test synthesize_speech returns None when TTS fails."""

    from pocketpaw.tools.builtin.voice import synthesize_speech

    mock_settings = MagicMock()
    mock_settings.tts_provider = "elevenlabs"
    mock_settings.elevenlabs_api_key = None  # Missing key triggers error

    with patch("pocketpaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        result = await synthesize_speech("Test")

    assert result is None


async def test_synthesize_speech_checks_execute_error_result():
    """Test synthesize_speech returns None when execute() returns an error string."""
    from pocketpaw.tools.builtin.voice import synthesize_speech

    with patch("pocketpaw.tools.builtin.voice.TextToSpeechTool") as mock_tool_cls:
        mock_tool = MagicMock()
        mock_tool.execute = AsyncMock(return_value="Error: provider unavailable")
        mock_tool._last_generated_path = "/tmp/tts_should_not_be_used.mp3"
        mock_tool_cls.return_value = mock_tool

        result = await synthesize_speech("Test")

    assert result is None
