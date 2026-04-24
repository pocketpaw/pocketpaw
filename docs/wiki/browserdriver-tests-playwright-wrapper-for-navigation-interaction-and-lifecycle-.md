---
{
  "title": "BrowserDriver Tests: Playwright Wrapper for Navigation, Interaction, and Lifecycle Management",
  "summary": "This test suite validates `BrowserDriver`, PocketPaw's thin wrapper around the Playwright async API. Tests cover initialization modes (headless/headful), async context manager lifecycle, browser launch and viewport configuration, all navigation and interaction operations (navigate, click, type, scroll), snapshot generation, screenshot capture, and defensive checks for using the driver before it's launched.",
  "concepts": [
    "BrowserDriver",
    "Playwright",
    "async_playwright",
    "RefMap",
    "accessibility tree",
    "headless mode",
    "context manager",
    "navigate",
    "click",
    "fill",
    "viewport",
    "RuntimeError"
  ],
  "categories": [
    "browser automation",
    "testing",
    "Playwright",
    "driver",
    "test"
  ],
  "source_docs": [
    "0ff996c240a39ca6"
  ],
  "backlinks": null,
  "word_count": 452,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`BrowserDriver` is the low-level Playwright adapter in PocketPaw's browser automation stack. It handles the mechanics of launching a Chromium browser, managing a single page, performing actions, and generating accessibility tree snapshots that the AI agent reads as its view of the page. This test file uses mock Playwright objects to verify the driver's behavior without requiring a real browser.

## Initialization and Context Manager

```python
class TestBrowserDriverInit:
    def test_driver_init(self):
        driver = BrowserDriver()
        assert driver.headless is True
        assert driver._browser is None
        assert driver._page is None

class TestBrowserDriverContextManager:
    async def test_context_manager_launches_browser(self):
        async with driver:
            mock_launch.assert_called_once()
        mock_close.assert_called_once()
```

The context manager test verifies that `launch()` and `close()` are always paired, preventing browser process leaks even if an exception occurs inside the `async with` block.

## Browser Launch and Playwright Mocking

```python
async def test_launch_creates_browser_and_page(self):
    mock_pw_cm = MagicMock()
    mock_pw_cm.start = AsyncMock(return_value=mock_playwright)
    with patch("playwright.async_api.async_playwright", return_value=mock_pw_cm):
        await driver.launch()
        assert driver._browser is not None
        assert driver._page is not None
```

The comment "Fixed mocking for async_playwright().start() pattern" in the file header indicates a non-trivial mocking challenge: the test explicitly mocks the `.start()` coroutine rather than `__aenter__`, which is the pattern `BrowserDriver` actually uses. This is a common source of test breakage when upgrading Playwright versions. The viewport test verifies that `new_context` is called with a `viewport` kwarg — Playwright defaults to no viewport which causes inconsistent accessibility tree snapshots across screen sizes.

## Navigation with RefMap

```python
async def test_navigate_returns_snapshot_and_refmap(self):
    result = await driver.navigate("https://example.com")
    assert isinstance(result.snapshot, str)
    assert isinstance(result.refmap, RefMap)
    assert "[ref=1]" in result.snapshot
```

The navigate result carries both a human-readable snapshot string and a `RefMap` that maps numeric references to Playwright locator strings. The `[ref=1]` assertion verifies that interactive elements are tagged with reference numbers, which is the mechanism by which an agent says "click [ref=3]" and the driver knows which element to target.

## Defensive Pre-Launch Checks

```python
async def test_navigate_requires_page(self):
    driver = BrowserDriver()  # not launched
    with pytest.raises(RuntimeError, match="Browser not launched"):
        await driver.navigate("https://example.com")
```

Every action method raises `RuntimeError` if called before `launch()`. This prevents silent failures where the driver object exists but no browser is running — a common mistake when the context manager isn't used.

## Type Behavior and Click Ref Validation

The driver uses Playwright's `fill()` method for text input, which atomically clears the field and enters new text, preventing the bug of appending text to a pre-filled input on retries. The `test_click_invalid_ref` test verifies that an invalid ref produces a clear `ValueError` rather than a cryptic Playwright `TimeoutError`.

## Known Gaps

No test covers `test_scroll_invalid_direction` — the AST lists it but the direction validation behavior isn't visible in the source excerpt. Screenshot tests verify only that a path string is returned, not that the file actually exists on disk.