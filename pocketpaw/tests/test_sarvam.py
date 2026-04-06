# Tests for Sarvam AI integrations: TTS, STT, OCR, Translate.
# Created: 2026-02-16

from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(**overrides):
    """Return a mock Settings with Sarvam defaults."""
    defaults = {
        "sarvam_api_key": "test-sarvam-key",
        "sarvam_tts_model": "bulbul:v3",
        "sarvam_tts_speaker": "shubh",
        "sarvam_tts_language": "hi-IN",
        "sarvam_stt_model": "saaras:v3",
        "tts_provider": "sarvam",
        "tts_voice": "alloy",
        "stt_provider": "sarvam",
        "ocr_provider": "sarvam",
        "openai_api_key": "test-openai-key",
        "elevenlabs_api_key": None,
        "stt_model": "whisper-1",
    }
    defaults.update(overrides)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


# ===========================================================================
# TranslateTool
# ===========================================================================


class TestTranslateTool:
    """Tests for the Sarvam translate tool."""

    def _make_tool(self):
        from pocketpaw.tools.builtin.translate import TranslateTool

        return TranslateTool()

    def test_definition(self):
        tool = self._make_tool()
        assert tool.name == "translate"
        assert tool.trust_level == "standard"
        params = tool.parameters
        assert "text" in params["properties"]
        assert "target_language" in params["properties"]
        assert params["required"] == ["text", "target_language"]

    @patch("pocketpaw.tools.builtin.translate.get_settings")
    async def test_no_api_key_returns_error(self, mock_gs):
        mock_gs.return_value = _mock_settings(sarvam_api_key=None)
        tool = self._make_tool()
        result = await tool.execute(text="hello", target_language="hi-IN")
        assert "error" in result.lower()
        assert "SARVAM_API_KEY" in result

    @patch("pocketpaw.tools.builtin.translate.get_settings")
    async def test_empty_text_returns_error(self, mock_gs):
        mock_gs.return_value = _mock_settings()
        tool = self._make_tool()
        result = await tool.execute(text="   ", target_language="hi-IN")
        assert "error" in result.lower()

    @patch("pocketpaw.tools.builtin.translate.get_settings")
    async def test_success_mock(self, mock_gs):
        mock_gs.return_value = _mock_settings()
        tool = self._make_tool()

        mock_response = MagicMock()
        mock_response.translated_text = "नमस्ते दुनिया"

        mock_client = MagicMock()
        mock_client.text.translate.return_value = mock_response

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            # Patch asyncio.to_thread to call sync
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                result = await tool.execute(text="Hello world", target_language="hi-IN")

        assert "नमस्ते दुनिया" in result
        assert "hi-IN" in result

    @patch("pocketpaw.tools.builtin.translate.get_settings")
    async def test_mode_formal(self, mock_gs):
        mock_gs.return_value = _mock_settings()
        tool = self._make_tool()

        mock_response = MagicMock()
        mock_response.translated_text = "translated text"

        mock_client = MagicMock()
        mock_client.text.translate.return_value = mock_response

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                result = await tool.execute(text="hi", target_language="ta-IN", mode="formal")

        assert "formal" in result

    @patch("pocketpaw.tools.builtin.translate.get_settings")
    async def test_sdk_error_propagates(self, mock_gs):
        mock_gs.return_value = _mock_settings()
        tool = self._make_tool()

        mock_client = MagicMock()
        mock_client.text.translate.side_effect = RuntimeError("SDK connection error")

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                result = await tool.execute(text="hi", target_language="hi-IN")
        assert "error" in result.lower()

    @patch("pocketpaw.tools.builtin.translate.get_settings")
    async def test_api_error(self, mock_gs):
        mock_gs.return_value = _mock_settings()
        tool = self._make_tool()

        mock_client = MagicMock()
        mock_client.text.translate.side_effect = RuntimeError("API error 429")

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                result = await tool.execute(text="hello", target_language="hi-IN")

        assert "error" in result.lower()


# ===========================================================================
# Sarvam TTS (in voice.py)
# ===========================================================================


class TestSarvamTTS:
    """Tests for Sarvam TTS via TextToSpeechTool."""

    def _make_tool(self):
        from pocketpaw.tools.builtin.voice import TextToSpeechTool

        return TextToSpeechTool()

    @patch("pocketpaw.tools.builtin.voice.get_settings")
    async def test_no_api_key_returns_error(self, mock_gs):
        mock_gs.return_value = _mock_settings(sarvam_api_key=None, tts_provider="sarvam")
        tool = self._make_tool()
        result = await tool.execute(text="hello")
        assert "error" in result.lower()
        assert "SARVAM_API_KEY" in result

    @patch("pocketpaw.tools.builtin.voice._get_audio_dir")
    @patch("pocketpaw.tools.builtin.voice.get_settings")
    async def test_success_mock(self, mock_gs, mock_dir, tmp_path):
        mock_gs.return_value = _mock_settings(tts_provider="sarvam")
        mock_dir.return_value = tmp_path

        tool = self._make_tool()

        import base64

        raw_audio = b"\x00\x01\x02" * 100
        b64_audio = base64.b64encode(raw_audio).decode()

        mock_response = MagicMock()
        mock_response.audios = [b64_audio]

        mock_client = MagicMock()
        mock_client.text_to_speech.convert.return_value = mock_response

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                result = await tool.execute(text="namaste")

        assert "Audio generated" in result
        assert "300 bytes" in result
        assert "<!-- media:" in result

    @patch("pocketpaw.tools.builtin.voice._get_audio_dir")
    @patch("pocketpaw.tools.builtin.voice.get_settings")
    async def test_bytes_response(self, mock_gs, mock_dir, tmp_path):
        """SDK returns base64-encoded audio in .audios list."""
        mock_gs.return_value = _mock_settings(tts_provider="sarvam")
        mock_dir.return_value = tmp_path
        tool = self._make_tool()

        import base64

        raw_audio = b"\xff\xd8" * 50
        b64_audio = base64.b64encode(raw_audio).decode()

        mock_response = MagicMock()
        mock_response.audios = [b64_audio]
        mock_client = MagicMock()
        mock_client.text_to_speech.convert.return_value = mock_response

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                result = await tool.execute(text="test")

        assert "Audio generated" in result

    @patch("pocketpaw.tools.builtin.voice.get_settings")
    async def test_custom_speaker(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(tts_provider="sarvam")
        tool = self._make_tool()

        import base64

        b64_audio = base64.b64encode(b"\x00" * 10).decode()
        mock_response = MagicMock()
        mock_response.audios = [b64_audio]
        mock_client = MagicMock()
        mock_client.text_to_speech.convert.return_value = mock_response

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("pocketpaw.tools.builtin.voice._get_audio_dir", return_value=tmp_path):
                    await tool.execute(text="hi", voice="kriti")

        # Verify speaker was passed correctly
        assert mock_client.text_to_speech.convert.call_args is not None

    @patch("pocketpaw.tools.builtin.voice.get_settings")
    async def test_sdk_not_installed(self, mock_gs):
        mock_gs.return_value = _mock_settings(tts_provider="sarvam")
        tool = self._make_tool()

        with patch("asyncio.to_thread", side_effect=ImportError("No module sarvamai")):
            result = await tool.execute(text="hello")
        assert "error" in result.lower()

    @patch("pocketpaw.tools.builtin.voice.get_settings")
    async def test_unknown_provider_error(self, mock_gs):
        mock_gs.return_value = _mock_settings(tts_provider="invalid_provider")
        tool = self._make_tool()
        result = await tool.execute(text="hello")
        assert "error" in result.lower()
        assert "sarvam" in result.lower()


# ===========================================================================
# Sarvam STT (in stt.py)
# ===========================================================================


class TestSarvamSTT:
    """Tests for Sarvam STT via SpeechToTextTool."""

    def _make_tool(self):
        from pocketpaw.tools.builtin.stt import SpeechToTextTool

        return SpeechToTextTool()

    @patch("pocketpaw.tools.builtin.stt.get_settings")
    async def test_no_api_key_returns_error(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(sarvam_api_key=None, stt_provider="sarvam")
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)
        tool = self._make_tool()
        result = await tool.execute(audio_file=str(audio_file))
        assert "error" in result.lower()
        assert "SARVAM_API_KEY" in result

    @patch("pocketpaw.tools.builtin.stt.get_settings")
    async def test_file_not_found(self, mock_gs):
        mock_gs.return_value = _mock_settings(stt_provider="sarvam")
        tool = self._make_tool()
        result = await tool.execute(audio_file="/nonexistent/file.wav")
        assert "error" in result.lower()
        assert "not found" in result.lower()

    @patch("pocketpaw.tools.builtin.stt._get_transcripts_dir")
    @patch("pocketpaw.tools.builtin.stt.get_settings")
    async def test_success_mock(self, mock_gs, mock_tdir, tmp_path):
        mock_gs.return_value = _mock_settings(stt_provider="sarvam")
        mock_tdir.return_value = tmp_path

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"transcript": "यह एक टेस्ट है"}
        mock_resp.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        patch_target = "pocketpaw.tools.builtin.stt.httpx.AsyncClient"
        with patch(patch_target, return_value=mock_client_instance):
            result = await tool.execute(audio_file=str(audio_file))

        assert "यह एक टेस्ट है" in result

    @patch("pocketpaw.tools.builtin.stt._get_transcripts_dir")
    @patch("pocketpaw.tools.builtin.stt.get_settings")
    async def test_mode_translit(self, mock_gs, mock_tdir, tmp_path):
        mock_gs.return_value = _mock_settings(stt_provider="sarvam")
        mock_tdir.return_value = tmp_path

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"transcript": "yeh ek test hai"}
        mock_resp.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        patch_target = "pocketpaw.tools.builtin.stt.httpx.AsyncClient"
        with patch(patch_target, return_value=mock_client_instance):
            result = await tool.execute(
                audio_file=str(audio_file), mode="translit", language="hi-IN"
            )

        assert "yeh ek test hai" in result
        assert "mode=translit" in result

    @patch("pocketpaw.tools.builtin.stt.get_settings")
    async def test_stt_http_error(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(stt_provider="sarvam")

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        tool = self._make_tool()

        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited", request=MagicMock(), response=mock_resp
        )

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        patch_target = "pocketpaw.tools.builtin.stt.httpx.AsyncClient"
        with patch(patch_target, return_value=mock_client_instance):
            result = await tool.execute(audio_file=str(audio_file))

        assert "error" in result.lower()
        assert "429" in result

    @patch("pocketpaw.tools.builtin.stt.get_settings")
    async def test_no_speech_detected(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(stt_provider="sarvam")

        audio_file = tmp_path / "silence.wav"
        audio_file.write_bytes(b"\x00" * 100)

        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"transcript": ""}
        mock_resp.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        patch_target = "pocketpaw.tools.builtin.stt.httpx.AsyncClient"
        with patch(patch_target, return_value=mock_client_instance):
            result = await tool.execute(audio_file=str(audio_file))

        assert "no speech" in result.lower()

    @patch("pocketpaw.tools.builtin.stt.get_settings")
    async def test_unknown_provider_error(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(stt_provider="invalid")
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)
        tool = self._make_tool()
        result = await tool.execute(audio_file=str(audio_file))
        assert "error" in result.lower()
        assert "sarvam" in result.lower()


# ===========================================================================
# Sarvam Vision OCR (in ocr.py)
# ===========================================================================


class TestSarvamOCR:
    """Tests for Sarvam Vision OCR via OCRTool."""

    def _make_tool(self):
        from pocketpaw.tools.builtin.ocr import OCRTool

        return OCRTool()

    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_no_api_key_returns_error(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(sarvam_api_key=None, ocr_provider="sarvam")
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        tool = self._make_tool()
        result = await tool.execute(image_path=str(img))
        assert "error" in result.lower()
        assert "SARVAM_API_KEY" in result

    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_file_not_found(self, mock_gs):
        mock_gs.return_value = _mock_settings(ocr_provider="sarvam")
        tool = self._make_tool()
        result = await tool.execute(image_path="/nonexistent/file.png")
        assert "error" in result.lower()
        assert "not found" in result.lower()

    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_unsupported_format(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(ocr_provider="sarvam")
        f = tmp_path / "test.xyz"
        f.write_bytes(b"\x00" * 100)
        tool = self._make_tool()
        result = await tool.execute(image_path=str(f))
        assert "error" in result.lower()
        assert "unsupported" in result.lower()

    @patch("pocketpaw.tools.builtin.ocr._get_ocr_output_dir")
    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_success_mock(self, mock_gs, mock_odir, tmp_path):
        mock_gs.return_value = _mock_settings(ocr_provider="sarvam")
        ocr_out = tmp_path / "ocr_out"
        ocr_out.mkdir()
        mock_odir.return_value = ocr_out

        img = tmp_path / "document.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)

        # Pre-create output markdown that the SDK "downloads"
        (ocr_out / "page_1.md").write_text("# Title\n\nExtracted text here.")

        tool = self._make_tool()

        mock_job = MagicMock()
        mock_job.upload_file = MagicMock()
        mock_job.start = MagicMock()
        mock_job.wait_until_complete = MagicMock()
        mock_job.download_output = MagicMock()

        mock_client = MagicMock()
        mock_client.document_intelligence.create_job.return_value = mock_job

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                result = await tool.execute(image_path=str(img))

        assert "Extracted text here" in result
        assert "document.png" in result

    @patch("pocketpaw.tools.builtin.ocr._get_ocr_output_dir")
    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_pdf_support(self, mock_gs, mock_odir, tmp_path):
        mock_gs.return_value = _mock_settings(ocr_provider="sarvam")
        ocr_out = tmp_path / "ocr_out"
        ocr_out.mkdir()
        mock_odir.return_value = ocr_out

        pdf = tmp_path / "document.pdf"
        pdf.write_bytes(b"%PDF-1.4" + b"\x00" * 100)

        (ocr_out / "page_1.md").write_text("PDF text content")

        tool = self._make_tool()

        mock_job = MagicMock()
        mock_client = MagicMock()
        mock_client.document_intelligence.create_job.return_value = mock_job

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                result = await tool.execute(image_path=str(pdf))

        assert "PDF text content" in result

    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_openai_rejects_pdf(self, mock_gs, tmp_path):
        """OpenAI Vision does not support PDF — should return helpful error."""
        mock_gs.return_value = _mock_settings(ocr_provider="openai")
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF" + b"\x00" * 100)
        tool = self._make_tool()
        result = await tool.execute(image_path=str(pdf))
        assert "error" in result.lower()
        assert "sarvam" in result.lower()

    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_sdk_not_installed(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(ocr_provider="sarvam")
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        tool = self._make_tool()

        with patch("asyncio.to_thread", side_effect=ImportError("No module sarvamai")):
            result = await tool.execute(image_path=str(img))
        assert "error" in result.lower()

    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_no_text_detected(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(ocr_provider="sarvam")
        ocr_out = tmp_path / "ocr_out"
        ocr_out.mkdir()

        img = tmp_path / "blank.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)

        tool = self._make_tool()

        mock_job = MagicMock()
        mock_client = MagicMock()
        mock_client.document_intelligence.create_job.return_value = mock_job

        with patch("sarvamai.SarvamAI", return_value=mock_client):
            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch("pocketpaw.tools.builtin.ocr._get_ocr_output_dir", return_value=ocr_out):
                    result = await tool.execute(image_path=str(img))

        assert "no text" in result.lower()

    @patch("pocketpaw.tools.builtin.ocr.get_settings")
    async def test_unknown_provider_error(self, mock_gs, tmp_path):
        mock_gs.return_value = _mock_settings(ocr_provider="invalid")
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        tool = self._make_tool()
        result = await tool.execute(image_path=str(img))
        assert "error" in result.lower()


# ===========================================================================
# Policy
# ===========================================================================


class TestSarvamPolicy:
    """Tests for translate policy group."""

    def test_translate_group_exists(self):
        from pocketpaw.tools.policy import TOOL_GROUPS

        assert "group:translate" in TOOL_GROUPS
        assert "translate" in TOOL_GROUPS["group:translate"]

    def test_translate_in_voice_group(self):
        from pocketpaw.tools.policy import TOOL_GROUPS

        assert "text_to_speech" in TOOL_GROUPS["group:voice"]
        assert "speech_to_text" in TOOL_GROUPS["group:voice"]

    def test_policy_allows_translate(self):
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(profile="full")
        assert policy.is_tool_allowed("translate")

    def test_policy_denies_translate(self):
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(profile="full", deny=["group:translate"])
        assert not policy.is_tool_allowed("translate")


# ===========================================================================
# Config
# ===========================================================================


class TestSarvamConfig:
    """Tests for Sarvam config fields."""

    def test_default_values(self):
        from pocketpaw.config import Settings

        s = Settings()
        assert s.sarvam_api_key is None
        assert s.sarvam_tts_model == "bulbul:v3"
        assert s.sarvam_tts_speaker == "shubh"
        assert s.sarvam_tts_language == "hi-IN"
        assert s.sarvam_stt_model == "saaras:v3"
        assert s.stt_provider == "openai"
        assert s.ocr_provider == "openai"

    def test_tts_provider_description_includes_sarvam(self):
        from pocketpaw.config import Settings

        field = Settings.model_fields["tts_provider"]
        assert "sarvam" in field.description.lower()


# ===========================================================================
# Registration
# ===========================================================================


class TestSarvamRegistration:
    """Tests for TranslateTool registration in __init__.py."""

    def test_translate_in_lazy_imports(self):
        from pocketpaw.tools.builtin import _LAZY_IMPORTS

        assert "TranslateTool" in _LAZY_IMPORTS
        assert _LAZY_IMPORTS["TranslateTool"] == (".translate", "TranslateTool")

    def test_translate_importable(self):
        from pocketpaw.tools.builtin.translate import TranslateTool

        tool = TranslateTool()
        assert tool.name == "translate"


# ===========================================================================
# Helpers
# ===========================================================================


async def _fake_to_thread(func, /, *args, **kwargs):
    """Drop-in replacement for asyncio.to_thread that runs func synchronously."""
    return func(*args, **kwargs)
