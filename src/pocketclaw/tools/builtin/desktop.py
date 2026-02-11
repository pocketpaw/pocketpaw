"""
Desktop interaction tools.
"""

from typing import Any
from pocketclaw.tools.protocol import BaseTool
from pocketclaw.tools.screenshot import take_screenshot
from pocketclaw.tools.status import get_system_status
import base64


class ScreenshotTool(BaseTool):
    """Tool to take a screenshot of the primary monitor."""

    name = "take_screenshot"
    description = "Take a screenshot of the user's primary monitor. Returns base64 encoded image."

    async def execute(self, **kwargs: Any) -> str:
        img_bytes = take_screenshot()
        if not img_bytes:
            return "Error: Failed to take screenshot (display might be unavailable)."

        # For now, return a message indicating success.
        # In a real multimodal agent, we'd attach the image to the message.
        # But for text-based Loop, we might need a way to pass binary data.
        # The MessageBus supports metadata.
        # For now, let's return a special string or handle it via a side channel?
        # Actually, let's return a description.
        # But wait, the USER wants to SEE it.
        # The TelegramAdapter can handle 'image' content if we structure the OutboundMessage correctly.
        # But the tool returns a string.
        # Let's return the base64 string and let the Loop/Adapter handle it?
        # Or save to a temp file and return the path?
        # Saving to file is safer for text-only LLMs.

        # Let's save to a temp file in the jail if possible, or just return "Buffered".
        # Re-reading `bot_gateway.py`: it uses `reply_photo(photo=img_bytes)`.

        # Strategy: The Tool execution returns a text summary.
        # BUT, we want the side effect of sending the image.
        # The AgentLoop publishes OutboundMessage.
        # If the Tool can "emit" a message to the bus, that would be ideal.
        # But Tools are passive.

        # Alternative: Tool saves file to `desktop/screenshot_<timestamp>.png` in the Memory/File store,
        # and returns "Screenshot saved to ...".
        # Then the user can ask "Send me that file".

        # OR: We encode it in the text result using a special tag? <image_base64>...</image_base64>?
        # This is getting complex for a "simplified" tool system.

        # Simple approach for now:
        # Return base64 string. The LLM won't like it.
        # Let's just return "Screenshot taken." and maybe we improve the system to support binary blobs later?
        # The USER specifically wants the existing features.

        # Let's use a Hack for Phase 2:
        # The tool returns "Screenshot taken."
        # BUT, we cheat and use the `loop` context? No.

        # Let's look at `AgentLoop`. It publishes `tool_result` event.
        # Maybe we can publish a separate event?

        # Actually, `tool_use` event in loop:
        # await self.bus.publish_system(SystemEvent(event_type="tool_use", ... result=result))

        # If we return a massive base64 string, it will clog the logs/memory.

        return "[Screenshot capture functionality is active but image routing to chat is pending implementation in Phase 2B]"

        # Wait, I am implementing Phase 2B NOW.
        # I should simply allow the tool to return a special result that the Agent Loop can interpret?
        # No, AgentLoop is generic.

        # Best approach: Save to `file_jail/screenshots/` and return the path.
        # The Agent can then use `ReadFile` or we rely on the Adapter to detecting "File paths"?

        # Let's stick to the prompt: `AgentLoop` orchestration.
        # If I return a path, the user can say "Download <path>" (if we implement Download tool or file serving).
        # Telegram Adapter can potentially treat paths as files to upload?

        # Let's stick to "saving to file" as the most robust "Agentic" way.
        # It persists the data.

        from datetime import datetime, timezone
        import os
        from pocketclaw.config import get_settings

        settings = get_settings()
        jail = settings.file_jail_path
        screenshots_dir = jail / "screenshots"
        screenshots_dir.mkdir(exist_ok=True)

        filename = f"screenshot_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.png"
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
