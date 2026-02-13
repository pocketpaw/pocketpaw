# Speech-to-Text tool — transcribe audio via OpenAI Whisper API.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from pocketclaw.config import get_config_dir, get_settings
from pocketclaw.tools.protocol import BaseTool

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
            "Transcribe an audio file to text using OpenAI Whisper. "
            "Supports mp3, mp4, mpeg, mpga, m4a, wav, webm formats. "
            "Transcript is also saved to ~/.pocketclaw/generated/transcripts/."
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
                        "Language code (ISO 639-1, e.g. 'en', 'es', 'fr'). "
                        "Auto-detected if not specified."
                    ),
                },
            },
            "required": ["audio_file"],
        }

    async def execute(self, audio_file: str, language: str | None = None) -> str:
        settings = get_settings()
        api_key = settings.openai_api_key
        if not api_key:
            return self._error("OpenAI API key not configured. Set POCKETCLAW_OPENAI_API_KEY.")

        audio_path = Path(audio_file).expanduser()
        if not audio_path.exists():
            return self._error(f"Audio file not found: {audio_path}")

        max_size = 25 * 1024 * 1024  # 25 MB Whisper limit
        if audio_path.stat().st_size > max_size:
            return self._error(
                f"Audio file too large ({audio_path.stat().st_size / 1024 / 1024:.1f} MB). "
                "Whisper API limit is 25 MB."
            )

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

            # Save transcript to file
            filename = f"stt_{uuid.uuid4().hex[:8]}.txt"
            output_path = _get_transcripts_dir() / filename
            output_path.write_text(text)

            return f"Transcription ({audio_path.name}):\n\n{text}\n\nSaved to: {output_path}"

        except httpx.HTTPStatusError as e:
            return self._error(f"Whisper API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"Transcription failed: {e}")
