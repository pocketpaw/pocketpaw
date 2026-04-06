"""Screenshot tool."""

import io

try:
    import pyautogui

    PYAUTOGUI_AVAILABLE = True
except Exception:
    PYAUTOGUI_AVAILABLE = False


def take_screenshot() -> bytes | None:
    """Take a screenshot and return as bytes."""
    if not PYAUTOGUI_AVAILABLE:
        return None

    try:
        # Take screenshot
        screenshot = pyautogui.screenshot()

        # Convert to bytes
        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG")
        buffer.seek(0)

        return buffer.getvalue()
    except Exception:
        # Common on headless servers or when display is not available
        return None
