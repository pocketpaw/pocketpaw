# Translation tool — translate text via Sarvam AI Mayura/Translate API.
# Created: 2026-02-16
# Supports 23 Indian languages + English.

import logging
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class TranslateTool(BaseTool):
    """Translate text between English and Indian languages using Sarvam AI."""

    @property
    def name(self) -> str:
        return "translate"

    @property
    def description(self) -> str:
        return (
            "Translate text between English and 22 Indian languages using Sarvam AI. "
            "Supports Hindi, Bengali, Tamil, Telugu, Marathi, Gujarati, Kannada, Malayalam, "
            "Odia, Punjabi, Assamese, Urdu, Nepali, Sanskrit, Sindhi, Kashmiri, Konkani, "
            "Dogri, Bodo, Maithili, Manipuri, Santali + English. "
            "Modes: formal, modern-colloquial, classic-colloquial, code-mixed."
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
                    "description": "Text to translate (max 2000 chars)",
                },
                "target_language": {
                    "type": "string",
                    "description": (
                        "Target language BCP-47 code (e.g. 'hi-IN' for Hindi, "
                        "'ta-IN' for Tamil, 'bn-IN' for Bengali, 'en-IN' for English)"
                    ),
                },
                "source_language": {
                    "type": "string",
                    "description": (
                        "Source language BCP-47 code (default: 'auto' for auto-detect)"
                    ),
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "Translation mode: 'formal' (default), 'modern-colloquial', "
                        "'classic-colloquial', or 'code-mixed'"
                    ),
                },
            },
            "required": ["text", "target_language"],
        }

    async def execute(
        self,
        text: str,
        target_language: str,
        source_language: str = "auto",
        mode: str = "formal",
    ) -> str:
        settings = get_settings()
        api_key = settings.sarvam_api_key
        if not api_key:
            return self._error("Sarvam API key not configured. Set POCKETPAW_SARVAM_API_KEY.")

        if not text.strip():
            return self._error("No text provided to translate.")

        try:
            import asyncio

            from sarvamai import SarvamAI

            client = SarvamAI(api_subscription_key=api_key)

            response = await asyncio.to_thread(
                client.text.translate,
                input=text[:2000],
                source_language_code=source_language,
                target_language_code=target_language,
                mode=mode,
            )

            translated = response.translated_text

            return f"Translation ({source_language} -> {target_language}, {mode}):\n\n{translated}"

        except ImportError:
            return self._error("Sarvam SDK not installed. Run: pip install 'pocketpaw[sarvam]'")
        except Exception as e:
            return self._error(f"Translation failed: {e}")
