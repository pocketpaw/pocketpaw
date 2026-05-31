"""
Desktop interaction tools.
"""

from datetime import UTC
from typing import Any

from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.screenshot import take_screenshot
from pocketpaw.tools.status import get_system_status


class ScreenshotTool(BaseTool):
    """Tool to take a screenshot of the primary monitor."""

    name = "take_screenshot"
    description = "Take a screenshot of the user's primary monitor. Returns base64 encoded image."

    async def execute(self, **kwargs: Any) -> str:
        img_bytes = take_screenshot()
        if not img_bytes:
            return "Error: Failed to take screenshot (display might be unavailable)."

        # Save screenshot to file jail for retrieval.
        # The agent can deliver it via deliver_artifact or the user can
        # download it from the Files panel in the dashboard.

        from datetime import datetime

        from pocketpaw.config import get_settings

        settings = get_settings()
        jail = settings.file_jail_path
        screenshots_dir = jail / "screenshots"
        screenshots_dir.mkdir(exist_ok=True)

        filename = f"screenshot_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}.png"
        path = screenshots_dir / filename

        with open(path, "wb") as f:
            f.write(img_bytes)

        return f"Screenshot saved to {path}"


class StatusTool(BaseTool):
    """Tool to get system status."""

    name = "get_status"
    description = "Get current system status (CPU, RAM, Disk, Battery)."

    async def execute(self, **kwargs: Any) -> str:
        return get_system_status()
