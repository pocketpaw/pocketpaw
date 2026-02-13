# Image Generation tool — generate images via Google Gemini.
# Created: 2026-02-06
# Part of Phase 1 Quick Wins

import logging
import uuid
from pathlib import Path
from typing import Any

from pocketclaw.config import get_config_dir, get_settings
from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_generated_dir() -> Path:
    """Get (and create) the directory for generated images."""
    d = get_config_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


class ImageGenerateTool(BaseTool):
    """Generate images using Google Gemini (Nano Banana)."""

    @property
    def name(self) -> str:
        return "image_generate"

    @property
    def description(self) -> str:
        return (
            "Generate an image from a text prompt using Google Gemini. "
            "Returns the file path of the saved image. "
            "Supports aspect ratios like '1:1', '16:9', '9:16'."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text description of the image to generate",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio (default: '1:1'). Options: '1:1', '16:9', '9:16'",
                    "default": "1:1",
                },
                "size": {
                    "type": "string",
                    "description": "Output resolution hint (default: '1K')",
                    "default": "1K",
                },
            },
            "required": ["prompt"],
        }

    async def execute(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        size: str = "1K",
    ) -> str:
        """Generate an image from a text prompt."""
        settings = get_settings()

        if not settings.google_api_key:
            return self._error("Google API key not configured. Set POCKETCLAW_GOOGLE_API_KEY.")

        try:
            from google import genai
        except ImportError:
            return self._error(
                "google-genai package not installed. Install with: pip install 'pocketpaw[image]'"
            )

        try:
            client = genai.Client(api_key=settings.google_api_key)
            response = client.models.generate_images(
                model=settings.image_model,
                prompt=prompt,
                config=genai.types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio=aspect_ratio,
                ),
            )

            if not response.generated_images:
                return self._error("No image was generated. Try a different prompt.")

            image = response.generated_images[0].image
            out_dir = _get_generated_dir()
            filename = f"{uuid.uuid4()}.png"
            out_path = out_dir / filename
            image.save(out_path)

            logger.info("Generated image: %s", out_path)
            return (
                f"Image generated and saved to: {out_path}\n"
                f"Prompt: {prompt}\n"
                f"Aspect ratio: {aspect_ratio}"
            )

        except Exception as e:
            return self._error(f"Image generation failed: {e}")
