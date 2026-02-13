# Tests for Speech-to-Text tool (Sprint 24)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSpeechToTextToolSchema:
    """Test SpeechToTextTool properties and schema."""

    def test_name(self):
        from pocketclaw.tools.builtin.stt import SpeechToTextTool

        tool = SpeechToTextTool()
        assert tool.name == "speech_to_text"

    def test_trust_level(self):
        from pocketclaw.tools.builtin.stt import SpeechToTextTool

        tool = SpeechToTextTool()
        assert tool.trust_level == "standard"

    def test_parameters(self):
        from pocketclaw.tools.builtin.stt import SpeechToTextTool

        tool = SpeechToTextTool()
        params = tool.parameters
        assert "audio_file" in params["properties"]
        assert "language" in params["properties"]
        assert "audio_file" in params["required"]

    def test_description(self):
        from pocketclaw.tools.builtin.stt import SpeechToTextTool

        tool = SpeechToTextTool()
        assert "Whisper" in tool.description
        assert "transcribe" in tool.description.lower()


@pytest.fixture
def _mock_settings():
    settings = MagicMock()
    settings.openai_api_key = "test-key"
    settings.stt_model = "whisper-1"
    with patch("pocketclaw.tools.builtin.stt.get_settings", return_value=settings):
        yield settings


async def test_stt_no_api_key():
    from pocketclaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    settings = MagicMock()
    settings.openai_api_key = None
    with patch("pocketclaw.tools.builtin.stt.get_settings", return_value=settings):
        result = await tool.execute(audio_file="/tmp/test.mp3")
    assert result.startswith("Error:")
    assert "API key" in result


async def test_stt_file_not_found(_mock_settings):
    from pocketclaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    result = await tool.execute(audio_file="/nonexistent/audio.mp3")
    assert result.startswith("Error:")
    assert "not found" in result


async def test_stt_file_too_large(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.stt import SpeechToTextTool

    tool = SpeechToTextTool()
    big_file = tmp_path / "big.mp3"
    big_file.write_bytes(b"\x00" * (26 * 1024 * 1024))  # 26 MB
    result = await tool.execute(audio_file=str(big_file))
    assert result.startswith("Error:")
    assert "too large" in result


async def test_stt_success(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.stt import SpeechToTextTool

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
            "pocketclaw.tools.builtin.stt._get_transcripts_dir",
            return_value=tmp_path,
        ):
            result = await tool.execute(audio_file=str(audio_file))

    assert "Hello world" in result
    assert "Saved to:" in result


async def test_stt_with_language(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.stt import SpeechToTextTool

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
            "pocketclaw.tools.builtin.stt._get_transcripts_dir",
            return_value=tmp_path,
        ):
            result = await tool.execute(audio_file=str(audio_file), language="es")

    assert "Hola mundo" in result
    # Verify language was passed
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["data"]["language"] == "es"


async def test_stt_empty_transcript(_mock_settings, tmp_path):
    from pocketclaw.tools.builtin.stt import SpeechToTextTool

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
    from pocketclaw.tools.builtin.stt import SpeechToTextTool

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
