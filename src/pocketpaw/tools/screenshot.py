"""Screenshot tool."""

import io

try:
    import pyautogui

    PYAUTOGUI_AVAILABLE = True
except Exception:
    PYAUTOGUI_AVAILABLE = False


def take_screenshot() -> bytes | str:
    """Take a screenshot and return as bytes, or an error string on failure."""
    if not PYAUTOGUI_AVAILABLE:
        return (
            "Screenshot capture is unavailable: pyautogui is not installed. "
            "Install it with: pip install pocketpaw[screenshot]"
        )

    try:
        # Take screenshot
        screenshot = pyautogui.screenshot()

        # Convert to bytes
        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG")
        buffer.seek(0)

        return buffer.getvalue()
    except Exception as exc:
        # Common on headless servers or when display is not available
        return f"Screenshot failed: {exc}"
