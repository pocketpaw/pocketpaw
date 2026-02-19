# Speech-to-Text tool — transcribe audio via OpenAI Whisper API.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from pocketpaw.config import get_config_dir, get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_transcripts_dir() -> Path:
    """Get/create the transcripts output directory."""
    d = get_config_dir() / "generated" / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


class SpeechToTextTool(BaseTool):
    """Transcribe audio files to text using OpenAI Whisper API."""

    @property
    def name(self) -> str:
        return "speech_to_text"

    @property
    def description(self) -> str:
        return (
            "Transcribe an audio file to text. Supports OpenAI Whisper and "
            "Sarvam AI Saaras (23 Indian languages with transcribe/translate/translit modes). "
            "Formats: mp3, mp4, mpeg, mpga, m4a, wav, webm. "
            "Transcript saved to ~/.pocketpaw/generated/transcripts/."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "audio_file": {
                    "type": "string",
                    "description": "Path to the audio file to transcribe",
                },
                "language": {
                    "type": "string",
                    "description": (
                        "Language code (ISO 639-1 for Whisper, e.g. 'en'; "
                        "BCP-47 for Sarvam, e.g. 'hi-IN', 'ta-IN'). "
                        "Auto-detected if not specified."
                    ),
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "Sarvam STT output mode (Saaras v3 only): "
                        "'transcribe' (default), 'translate' (to English), "
                        "'verbatim', 'translit' (romanized), 'codemix'."
                    ),
                },
            },
            "required": ["audio_file"],
        }

    async def execute(
        self, audio_file: str, language: str | None = None, mode: str | None = None
    ) -> str:
        audio_path = Path(audio_file).expanduser()
        if not audio_path.exists():
            return self._error(f"Audio file not found: {audio_path}")

        max_size = 25 * 1024 * 1024  # 25 MB
        if audio_path.stat().st_size > max_size:
            return self._error(
                f"Audio file too large ({audio_path.stat().st_size / 1024 / 1024:.1f} MB). "
                "Max 25 MB."
            )

        settings = get_settings()
        provider = settings.stt_provider

        if provider == "sarvam":
            return await self._stt_sarvam(audio_path, language, mode)
        elif provider == "openai":
            return await self._stt_openai(audio_path, language)
        else:
            return self._error(f"Unknown STT provider: {provider}. Use 'openai' or 'sarvam'.")

    async def _stt_openai(self, audio_path: Path, language: str | None) -> str:
        """Transcribe via OpenAI Whisper API."""
        settings = get_settings()
        api_key = settings.openai_api_key
        if not api_key:
            return self._error("OpenAI API key not configured. Set POCKETPAW_OPENAI_API_KEY.")

        model = settings.stt_model

        try:
            data = {"model": model}
            if language:
                data["language"] = language

            async with httpx.AsyncClient(timeout=120) as client:
                with open(audio_path, "rb") as f:
                    resp = await client.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        data=data,
                        files={"file": (audio_path.name, f, "audio/mpeg")},
                    )
                    resp.raise_for_status()

            result = resp.json()
            text = result.get("text", "")

            if not text.strip():
                return "Transcription completed but no speech was detected in the audio."

            filename = f"stt_{uuid.uuid4().hex[:8]}.txt"
            output_path = _get_transcripts_dir() / filename
            output_path.write_text(text)

            return f"Transcription ({audio_path.name}):\n\n{text}\n\nSaved to: {output_path}"

        except httpx.HTTPStatusError as e:
            return self._error(f"Whisper API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"Transcription failed: {e}")

    async def _stt_sarvam(self, audio_path: Path, language: str | None, mode: str | None) -> str:
        """Transcribe via Sarvam AI Saaras STT API."""
        settings = get_settings()
        api_key = settings.sarvam_api_key
        if not api_key:
            return self._error("Sarvam API key not configured. Set POCKETPAW_SARVAM_API_KEY.")

        try:
            data: dict[str, str] = {"model": settings.sarvam_stt_model}
            if language:
                data["language_code"] = language
            if mode:
                data["mode"] = mode

            async with httpx.AsyncClient(timeout=120) as client:
                with open(audio_path, "rb") as f:
                    resp = await client.post(
                        "https://api.sarvam.ai/speech-to-text",
                        headers={"api-subscription-key": api_key},
                        data=data,
                        files={"file": (audio_path.name, f)},
                    )
                    resp.raise_for_status()

            result = resp.json()
            text = result.get("transcript", "")

            if not text.strip():
                return "Transcription completed but no speech was detected in the audio."

            filename = f"stt_{uuid.uuid4().hex[:8]}.txt"
            output_path = _get_transcripts_dir() / filename
            output_path.write_text(text)

            lang_info = f", lang={language}" if language else ""
            mode_info = f", mode={mode}" if mode else ""
            return (
                f"Transcription ({audio_path.name}{lang_info}{mode_info}):\n\n"
                f"{text}\n\nSaved to: {output_path}"
            )

        except httpx.HTTPStatusError as e:
            return self._error(f"Sarvam STT API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"Sarvam transcription failed: {e}")
