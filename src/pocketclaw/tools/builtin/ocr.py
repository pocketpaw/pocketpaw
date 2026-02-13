# OCR tool — extract text from images using OpenAI Vision API.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

import base64
import logging
from pathlib import Path
from typing import Any

import httpx

from pocketclaw.config import get_settings
from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# MIME types for common image formats
_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


class OCRTool(BaseTool):
    """Extract text from images using OpenAI Vision (GPT-4o)."""

    @property
    def name(self) -> str:
        return "ocr"

    @property
    def description(self) -> str:
        return (
            "Extract text from an image file using OCR (GPT-4o vision). "
            "Supports PNG, JPG, GIF, WebP. Good for screenshots, photos of documents, "
            "business cards, signs, etc."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Path to the image file",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Custom extraction prompt (default: extract all visible text). "
                        "Use for specific tasks like 'extract the phone number' or "
                        "'read the table data'."
                    ),
                },
            },
            "required": ["image_path"],
        }

    async def execute(
        self,
        image_path: str,
        prompt: str = (
            "Extract all visible text from this image. "
            "Preserve the layout and formatting as much as possible."
        ),
    ) -> str:
        settings = get_settings()

        image_file = Path(image_path).expanduser()
        if not image_file.exists():
            return self._error(f"Image file not found: {image_file}")

        suffix = image_file.suffix.lower()
        mime_type = _MIME_TYPES.get(suffix)
        if not mime_type:
            return self._error(
                f"Unsupported image format '{suffix}'. Supported: {', '.join(_MIME_TYPES.keys())}"
            )

        max_size = 20 * 1024 * 1024  # 20 MB
        if image_file.stat().st_size > max_size:
            return self._error("Image file too large (max 20 MB).")

        # Primary: OpenAI Vision API
        if settings.openai_api_key:
            return await self._ocr_openai(image_file, mime_type, prompt, settings.openai_api_key)

        # Fallback: pytesseract
        return await self._ocr_tesseract(image_file)

    async def _ocr_openai(self, image_file: Path, mime_type: str, prompt: str, api_key: str) -> str:
        """OCR via OpenAI GPT-4o vision."""
        try:
            image_data = base64.b64encode(image_file.read_bytes()).decode()
            data_url = f"data:{mime_type};base64,{image_data}"

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o",
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": data_url},
                                    },
                                ],
                            }
                        ],
                        "max_tokens": 4096,
                    },
                )
                resp.raise_for_status()

            result = resp.json()
            text = result["choices"][0]["message"]["content"]

            if not text.strip():
                return "No text detected in the image."

            return f"OCR result ({image_file.name}):\n\n{text}"

        except httpx.HTTPStatusError as e:
            return self._error(f"OpenAI Vision API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"OCR failed: {e}")

    async def _ocr_tesseract(self, image_file: Path) -> str:
        """Fallback OCR via pytesseract (offline)."""
        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            return self._error(
                "No OCR provider available. Either set POCKETCLAW_OPENAI_API_KEY "
                "for GPT-4o vision, or install pytesseract: pip install 'pocketpaw[ocr]'"
            )

        try:
            import asyncio

            image = Image.open(image_file)
            text = await asyncio.to_thread(pytesseract.image_to_string, image)

            if not text.strip():
                return "No text detected in the image."

            return f"OCR result ({image_file.name}):\n\n{text}"

        except Exception as e:
            return self._error(f"Tesseract OCR failed: {e}")
