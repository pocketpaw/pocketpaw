# Artifact delivery tool - sends files back to the user via their channel.
# Created: 2026-03-12

import mimetypes
from pathlib import Path
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.tools.fetch import is_safe_path
from pocketpaw.tools.protocol import BaseTool


class DeliverArtifactTool(BaseTool):
    """Send a file to the user through their current channel."""

    @property
    def name(self) -> str:
        return "deliver_artifact"

    @property
    def description(self) -> str:
        return (
            "Send a file (image, video, audio, PDF, etc.) to the user through "
            "their current channel. Use after creating or downloading a file "
            "that the user should receive."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to deliver",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional message to accompany the file",
                    "default": "",
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, caption: str = "") -> str:
        """Deliver a file to the user."""
        try:
            file_path = Path(path).expanduser().resolve()

            # Security: check file jail
            jail = get_settings().file_jail_path.resolve()
            if not is_safe_path(file_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")

            if not file_path.exists():
                return self._error(f"File not found: {path}")

            if not file_path.is_file():
                return self._error(f"Not a file: {path}")

            # Check file size (100MB limit)
            size = file_path.stat().st_size
            if size > 100 * 1024 * 1024:
                return self._error(f"File too large: {size / (1024 * 1024):.1f}MB (max 100MB)")

            # Detect MIME type for the caption
            mime, _ = mimetypes.guess_type(str(file_path))
            size_str = (
                f"{size / (1024 * 1024):.1f}MB" if size > 1024 * 1024 else f"{size / 1024:.1f}KB"
            )
            info = f"Delivering {file_path.name} ({mime or 'unknown'}, {size_str})"

            if caption:
                return self._media_result(str(file_path), f"{caption}\n{info}")
            return self._media_result(str(file_path), info)

        except Exception as e:
            return self._error(str(e))
