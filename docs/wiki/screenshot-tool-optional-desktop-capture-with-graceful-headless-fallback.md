---
{
  "title": "Screenshot Tool: Optional Desktop Capture with Graceful Headless Fallback",
  "summary": "The screenshot module provides a single function for capturing the current display as PNG bytes, using pyautogui when available and returning an actionable error string when it is not. The optional-import pattern ensures PocketPaw's core runtime does not require a GUI dependency, keeping server and headless deployments working without modification.",
  "concepts": [
    "screenshot",
    "pyautogui",
    "optional dependency",
    "headless server",
    "desktop tool",
    "PNG capture",
    "PYAUTOGUI_AVAILABLE",
    "graceful degradation",
    "bytes return type"
  ],
  "categories": [
    "tools",
    "desktop integration",
    "optional features"
  ],
  "source_docs": [
    "67e681585a4f384e"
  ],
  "backlinks": null,
  "word_count": 382,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw runs in contexts ranging from developer laptops with full GUI environments to headless cloud servers. The screenshot tool needs to work in both without crashing or requiring conditional installation logic at the call site. `screenshot.py` achieves this with a try/except import guard and a union return type.

## Optional Dependency Pattern

```python
try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
except Exception:
    PYAUTOGUI_AVAILABLE = False
```

The `except Exception` (rather than `except ImportError`) catches a broader class of failures -- for example, pyautogui may import successfully but fail to initialize on a headless server where no display is configured, raising a runtime exception during module load. Catching `Exception` ensures `PYAUTOGUI_AVAILABLE` is reliably `False` in any environment where screenshot capture cannot work.

## Return Type: bytes or str

`take_screenshot()` returns `bytes` on success (PNG-encoded screenshot) or a `str` error message on any failure. This union type keeps the caller's code simple: check the type, branch accordingly. It avoids raising exceptions for expected conditions (no display available) while still propagating enough information for the agent to surface a useful message to the user.

When pyautogui is not available, the error string directs the user to install the optional extra:

```
Screenshot capture is unavailable: pyautogui is not installed.
Install it with: pip install pocketpaw[screenshot]
```

## Failure Modes

Two failure paths are handled:

1. **Missing dependency** -- checked at module import time via `PYAUTOGUI_AVAILABLE`. Returns a user-readable string immediately without attempting the capture.
2. **Runtime capture failure** -- the `try/except` around `pyautogui.screenshot()` catches errors that occur even when pyautogui is installed. Common causes include headless servers (no X11/Wayland display), insufficient permissions, or display timeouts.

## Integration Notes

The module exposes only `take_screenshot()` as a top-level function rather than a `BaseTool` subclass. The actual tool wrapper that integrates with `ToolRegistry` lives elsewhere. This separation keeps the screenshot capture logic independently testable -- you can unit-test `take_screenshot` by mocking pyautogui without constructing a full `ToolRegistry`.

## Known Gaps

The PNG encoding uses an in-memory `io.BytesIO` buffer with no size cap. A very large screen at high DPI could produce multi-megabyte PNG files passed through the tool result pipeline. No compression quality settings or resolution downscaling are available. Additionally, multi-monitor setups always capture the primary display -- there is no parameter to target a specific monitor.