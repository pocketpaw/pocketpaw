# Image Generation tool — generate images via Google Gemini.
# Created: 2026-02-06
# Part of Phase 1 Quick Wins
# 2026-06-10: route by model family — gemini-*-image models go through
#   generate_content (free tier), imagen-* models through generate_images
#   (predict endpoint, paid tier only). Shared helper generate_image_file()
#   is also used by the EE media MCP server.

import logging
import uuid
from pathlib import Path
from typing import Any

from pocketpaw.config import get_config_dir, get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_generated_dir() -> Path:
    """Get (and create) the directory for generated images."""
    d = get_config_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_image_file(
    client: Any, model: str, prompt: str, aspect_ratio: str, out_path: Path
) -> str | None:
    """Generate one image with ``client`` and write it to ``out_path``.

    Returns an error message, or None on success. Routes by model family:
    ``imagen-*`` models only exist on the predict endpoint
    (``generate_images``), which Google restricts to paid-tier keys; Gemini
    image models (``gemini-2.5-flash-image`` etc.) use ``generate_content``
    and work on the free tier.
    """
    from google import genai

    if model.startswith("imagen"):
        response = client.models.generate_images(
            model=model,
            prompt=prompt,
            config=genai.types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=aspect_ratio,
            ),
        )
        if not response.generated_images:
            return "No image was generated. Try a different prompt."
        response.generated_images[0].image.save(out_path)
        return None

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=genai.types.ImageConfig(aspect_ratio=aspect_ratio),
        ),
    )
    parts = response.candidates[0].content.parts if response.candidates else []
    image_part = next(
        (
            p
            for p in parts
            if getattr(p, "inline_data", None) is not None
            and (p.inline_data.mime_type or "").startswith("image/")
        ),
        None,
    )
    if image_part is None:
        return "No image was generated. Try a different prompt."
    out_path.write_bytes(image_part.inline_data.data)
    return None


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
            return self._error("Google API key not configured. Set POCKETPAW_GOOGLE_API_KEY.")

        try:
            from google import genai
        except ImportError:
            return self._error(
                "google-genai package not installed. Install with: pip install 'pocketpaw[image]'"
            )

        try:
            client = genai.Client(api_key=settings.google_api_key)
            out_dir = _get_generated_dir()
            filename = f"{uuid.uuid4()}.png"
            out_path = out_dir / filename
            err = generate_image_file(client, settings.image_model, prompt, aspect_ratio, out_path)
            if err:
                return self._error(err)

            logger.info("Generated image: %s", out_path)
            return self._media_result(
                str(out_path),
                f"Image generated (prompt: {prompt}, aspect ratio: {aspect_ratio})",
            )

        except Exception as e:
            return self._error(f"Image generation failed: {e}")
