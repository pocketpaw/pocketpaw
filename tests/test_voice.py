# Tests for tools/builtin/voice.py
# Created: 2026-02-07

from unittest.mock import MagicMock, patch

from pocketclaw.tools.builtin.voice import TextToSpeechTool, _get_audio_dir

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

    with patch("pocketclaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        result = await tool.execute(text="Hello world")
        assert "Error" in result
        assert "OpenAI" in result


async def test_elevenlabs_no_key():
    tool = TextToSpeechTool()
    mock_settings = MagicMock()
    mock_settings.tts_provider = "elevenlabs"
    mock_settings.tts_voice = "test-voice-id"
    mock_settings.elevenlabs_api_key = None

    with patch("pocketclaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        result = await tool.execute(text="Hello world")
        assert "Error" in result
        assert "ElevenLabs" in result


async def test_unknown_provider():
    tool = TextToSpeechTool()
    mock_settings = MagicMock()
    mock_settings.tts_provider = "unknown"
    mock_settings.tts_voice = "x"

    with patch("pocketclaw.tools.builtin.voice.get_settings", return_value=mock_settings):
        result = await tool.execute(text="Hello")
        assert "Error" in result
        assert "Unknown TTS provider" in result


# ---------------------------------------------------------------------------
# Audio directory
# ---------------------------------------------------------------------------


def test_get_audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("pocketclaw.tools.builtin.voice.get_config_dir", lambda: tmp_path)
    d = _get_audio_dir()
    assert d.exists()
    assert d == tmp_path / "generated" / "audio"
