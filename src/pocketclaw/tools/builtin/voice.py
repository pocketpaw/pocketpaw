# Voice/TTS tool — text-to-speech via OpenAI or ElevenLabs.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from pocketclaw.config import get_config_dir, get_settings
from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_audio_dir() -> Path:
    """Get/create the audio output directory."""
    d = get_config_dir() / "generated" / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TextToSpeechTool(BaseTool):
    """Convert text to speech audio file."""

    @property
    def name(self) -> str:
        return "text_to_speech"

    @property
    def description(self) -> str:
        return (
            "Convert text to speech audio (MP3). Supports OpenAI TTS (tts-1 model) "
            "and ElevenLabs. Output saved to ~/.pocketclaw/generated/audio/."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to convert to speech",
                },
                "voice": {
                    "type": "string",
                    "description": (
                        "Voice name (OpenAI: alloy/echo/fable/onyx/nova/shimmer"
                        "; ElevenLabs: voice ID)"
                    ),
                },
            },
            "required": ["text"],
        }

    async def execute(self, text: str, voice: str | None = None) -> str:
        settings = get_settings()
        provider = settings.tts_provider
        voice = voice or settings.tts_voice

        if provider == "openai":
            return await self._tts_openai(text, voice)
        elif provider == "elevenlabs":
            return await self._tts_elevenlabs(text, voice)
        else:
            return self._error(f"Unknown TTS provider: {provider}. Use 'openai' or 'elevenlabs'.")

    async def _tts_openai(self, text: str, voice: str) -> str:
        """Generate speech using OpenAI TTS API."""
        settings = get_settings()
        api_key = settings.openai_api_key
        if not api_key:
            return self._error("OpenAI API key not configured. Set POCKETCLAW_OPENAI_API_KEY.")

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "tts-1",
                        "input": text[:4096],
                        "voice": voice,
                        "response_format": "mp3",
                    },
                )
                resp.raise_for_status()

            # Save to file
            filename = f"tts_{uuid.uuid4().hex[:8]}.mp3"
            output_path = _get_audio_dir() / filename
            output_path.write_bytes(resp.content)

            return f"Audio generated: {output_path} ({len(resp.content)} bytes)"

        except httpx.HTTPStatusError as e:
            return self._error(f"OpenAI TTS error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"TTS failed: {e}")

    async def _tts_elevenlabs(self, text: str, voice: str) -> str:
        """Generate speech using ElevenLabs API."""
        settings = get_settings()
        api_key = settings.elevenlabs_api_key
        if not api_key:
            return self._error(
                "ElevenLabs API key not configured. Set POCKETCLAW_ELEVENLABS_API_KEY."
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                    headers={
                        "xi-api-key": api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "text": text[:5000],
                        "model_id": "eleven_monolingual_v1",
                    },
                )
                resp.raise_for_status()

            # Save to file
            filename = f"tts_{uuid.uuid4().hex[:8]}.mp3"
            output_path = _get_audio_dir() / filename
            output_path.write_bytes(resp.content)

            return f"Audio generated: {output_path} ({len(resp.content)} bytes)"

        except httpx.HTTPStatusError as e:
            return self._error(f"ElevenLabs error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"TTS failed: {e}")
