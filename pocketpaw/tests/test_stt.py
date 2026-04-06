# Tests for Speech-to-Text tool (Sprint 24)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSpeechToTextToolSchema:
    """Test SpeechToTextTool properties and schema."""

    def test_name(self):
        from pocketpaw.tools.builtin.stt import SpeechToTextTool

        tool = SpeechToTextTool()
        assert tool.name == "speech_to_text"

    def test_trust_level(self):
        from pocketpaw.tools.builtin.stt import SpeechToTextTool

        tool = SpeechToTextTool()
        assert tool.trust_level == "standard"

    def test_parameters(self):
        from pocketpaw.tools.builtin.stt import SpeechToTextTool

        tool = SpeechToTextTool()
        params = tool.parameters
        assert "audio_file" in params["properties"]
        assert "language" in params["properties"]
        assert "audio_file" in params["required"]

    def test_description(self):
        from pocketpaw.tools.builtin.stt import SpeechToTextTool

        tool = SpeechToTextTool()
        assert "Whisper" in tool.description
        assert "transcribe" in tool.description.lower()


@pytest.fixture
def _mock_settings():
    settings = MagicMock()
    settings.openai_api_key = "test-key"
    settings.stt_model = "whisper-1"
    settings.stt_provider = "openai"
    with patch("pocketpaw.tools.builtin.stt.get_settings", return_value=settings):
        yield settings


async def test_stt_no_api_key(tmp_path):
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"\x00" * 100)
    settings = MagicMock()
    settings.openai_api_key = None
    settings.stt_provider = "openai"
    with patch("pocketpaw.tools.builtin.stt.get_settings", return_value=settings):
        result = await tool.execute(audio_file=str(audio_file))
    assert result.startswith("Error:")
    assert "API key" in result


async def test_stt_file_not_found(_mock_settings):
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    result = await tool.execute(audio_file="/nonexistent/audio.mp3")
    assert result.startswith("Error:")
    assert "not found" in result


async def test_stt_file_too_large(_mock_settings, tmp_path):
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    big_file = tmp_path / "big.mp3"
    big_file.write_bytes(b"\x00" * (26 * 1024 * 1024))  # 26 MB
    result = await tool.execute(audio_file=str(big_file))
    assert result.startswith("Error:")
    assert "too large" in result


async def test_stt_success(_mock_settings, tmp_path):
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"\x00" * 100)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"text": "Hello world, this is a test."}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with patch(
            "pocketpaw.tools.builtin.stt._get_transcripts_dir",
            return_value=tmp_path,
        ):
            result = await tool.execute(audio_file=str(audio_file))

    assert "Hello world" in result
    assert "Saved to:" in result


async def test_stt_with_language(_mock_settings, tmp_path):
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"\x00" * 100)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"text": "Hola mundo"}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with patch(
            "pocketpaw.tools.builtin.stt._get_transcripts_dir",
            return_value=tmp_path,
        ):
            result = await tool.execute(audio_file=str(audio_file), language="es")

    assert "Hola mundo" in result
    # Verify language was passed
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["data"]["language"] == "es"


async def test_stt_empty_transcript(_mock_settings, tmp_path):
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "silence.mp3"
    audio_file.write_bytes(b"\x00" * 100)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"text": ""}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await tool.execute(audio_file=str(audio_file))

    assert "no speech" in result.lower()


async def test_stt_api_error(_mock_settings, tmp_path):
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"\x00" * 100)

    import httpx as httpx_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.request = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx_mod.HTTPStatusError(
                "rate limited", request=mock_resp.request, response=mock_resp
            )
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await tool.execute(audio_file=str(audio_file))

    assert result.startswith("Error:")
    assert "429" in result


# ---------------------------------------------------------------------------
# ElevenLabs STT provider tests
# ---------------------------------------------------------------------------


async def test_elevenlabs_stt_success(tmp_path):
    """Test ElevenLabs STT provider transcribes audio successfully."""
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "voice.ogg"
    audio_file.write_bytes(b"\x00" * 500)

    mock_settings = MagicMock()
    mock_settings.stt_provider = "elevenlabs"
    mock_settings.stt_model = "eleven_multilingual_v2"
    mock_settings.elevenlabs_api_key = "test-elevenlabs-key"

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"text": "Hello from ElevenLabs STT"}
    mock_resp.raise_for_status = MagicMock()

    with patch("pocketpaw.tools.builtin.stt.get_settings", return_value=mock_settings):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch(
                "pocketpaw.tools.builtin.stt._get_transcripts_dir",
                return_value=tmp_path,
            ):
                result = await tool.execute(audio_file=str(audio_file))

    assert "Hello from ElevenLabs STT" in result
    assert "Saved to:" in result

    # Verify correct API endpoint and headers
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://api.elevenlabs.io/v1/speech-to-text"
    assert call_args[1]["headers"]["xi-api-key"] == "test-elevenlabs-key"
    assert call_args[1]["data"]["model_id"] == "eleven_multilingual_v2"


async def test_elevenlabs_stt_with_language(tmp_path):
    """Test ElevenLabs STT passes language parameter correctly."""
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "voice_es.mp3"
    audio_file.write_bytes(b"\x00" * 500)

    mock_settings = MagicMock()
    mock_settings.stt_provider = "elevenlabs"
    mock_settings.stt_model = "eleven_multilingual_v2"
    mock_settings.elevenlabs_api_key = "test-key"

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"text": "Hola desde ElevenLabs"}
    mock_resp.raise_for_status = MagicMock()

    with patch("pocketpaw.tools.builtin.stt.get_settings", return_value=mock_settings):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch(
                "pocketpaw.tools.builtin.stt._get_transcripts_dir",
                return_value=tmp_path,
            ):
                result = await tool.execute(audio_file=str(audio_file), language="es")

    assert "Hola desde ElevenLabs" in result
    # Verify language was passed
    call_kwargs = mock_client.post.call_args[1]
    assert call_kwargs["data"]["language"] == "es"


async def test_elevenlabs_stt_no_api_key(tmp_path):
    """Test ElevenLabs STT fails gracefully when API key is missing."""
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "voice.mp3"
    audio_file.write_bytes(b"\x00" * 100)

    mock_settings = MagicMock()
    mock_settings.stt_provider = "elevenlabs"
    mock_settings.elevenlabs_api_key = None

    with patch("pocketpaw.tools.builtin.stt.get_settings", return_value=mock_settings):
        result = await tool.execute(audio_file=str(audio_file))

    assert result.startswith("Error:")
    assert "ElevenLabs API key" in result


async def test_elevenlabs_stt_api_error(tmp_path):
    """Test ElevenLabs STT handles API errors gracefully."""
    from pocketpaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    audio_file = tmp_path / "voice.mp3"
    audio_file.write_bytes(b"\x00" * 100)

    mock_settings = MagicMock()
    mock_settings.stt_provider = "elevenlabs"
    mock_settings.stt_model = "eleven_multilingual_v2"
    mock_settings.elevenlabs_api_key = "test-key"

    import httpx as httpx_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.request = MagicMock()

    with patch("pocketpaw.tools.builtin.stt.get_settings", return_value=mock_settings):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                side_effect=httpx_mod.HTTPStatusError(
                    "unauthorized", request=mock_resp.request, response=mock_resp
                )
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(audio_file=str(audio_file))

    assert result.startswith("Error:")
    assert "401" in result
