# Playwright browser driver wrapper
# Changes: Added auto-install and system Chrome support
#
# Wraps Playwright browser automation with methods for navigate, click,
# type, scroll, snapshot, and screenshot.
# Uses system Chrome if available, auto-installs Chromium if needed.
"""Playwright browser driver wrapper."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .snapshot import AccessibilityNode, RefMap, SnapshotGenerator

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Playwright

logger = logging.getLogger(__name__)


@dataclass
class NavigationResult:
    """Result of a navigation or interaction that returns page state."""

    snapshot: str
    refmap: RefMap


class BrowserDriver:
    """Playwright browser driver wrapper.

    Provides a simplified async interface for browser automation
    with accessibility tree snapshots for LLM control.

    Usage:
        async with BrowserDriver() as driver:
            result = await driver.navigate("https://example.com")
            print(result.snapshot)
            await driver.click(ref=1)
    """

    # Default viewport size
    DEFAULT_VIEWPORT = {"width": 1280, "height": 720}

    # Scroll amount in pixels
    SCROLL_AMOUNT = 500

    def __init__(self, headless: bool = True) -> None:
        """Initialize the browser driver.

        Args:
            headless: Whether to run browser in headless mode (default True)
        """
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._refmap: RefMap = RefMap()
        self._snapshot_generator = SnapshotGenerator()

        # Verify playwright is installed early (fail fast with helpful message)
        try:
            import playwright  # noqa: F401
        except ImportError:
            from pocketclaw._compat import require_extra

            require_extra("playwright", "browser")

    async def __aenter__(self) -> BrowserDriver:
        """Async context manager entry - launches browser."""
        await self.launch()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit - closes browser."""
        await self.close()

    @property
    def is_launched(self) -> bool:
        """Check if browser is launched and ready."""
        return self._browser is not None and self._page is not None

    @property
    def current_url(self) -> str | None:
        """Get current page URL or None if not launched."""
        if self._page is None:
            return None
        return self._page.url

    async def launch(self) -> None:
        """Launch the browser.

        Tries in order:
        1. System Chrome (no download needed)
        2. Playwright's bundled Chromium (auto-installs if missing)
        """
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        # Try system Chrome first (no download needed for users)
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                channel="chrome",  # Use system Chrome
            )
            logger.info("Using system Chrome")
        except Exception as e:
            logger.debug(f"System Chrome not available: {e}")
            # Fall back to Playwright's Chromium
            try:
                self._browser = await self._playwright.chromium.launch(headless=self.headless)
                logger.info("Using Playwright Chromium")
            except Exception as install_error:
                # Chromium not installed - auto-install it
                if "Executable doesn't exist" in str(install_error):
                    logger.info("Installing Chromium browser (one-time download)...")
                    await self._install_chromium()
                    # Try again after install
                    self._browser = await self._playwright.chromium.launch(headless=self.headless)
                    logger.info("Using Playwright Chromium (freshly installed)")
                else:
                    raise

        context = await self._browser.new_context(viewport=self.DEFAULT_VIEWPORT)
        self._page = await context.new_page()

    async def _install_chromium(self) -> None:
        """Auto-install Playwright's Chromium browser."""
        logger.info("Downloading Chromium browser (~150MB)...")

        # Run playwright install chromium
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            raise RuntimeError(f"Failed to install Chromium: {error_msg}")

        logger.info("Chromium installed successfully")

    async def close(self) -> None:
        """Close the browser and cleanup resources."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None
        self._refmap = RefMap()

    def _require_page(self) -> Page:
        """Get page or raise if not launched."""
        if self._page is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    async def _take_snapshot(self) -> NavigationResult:
        """Take accessibility snapshot of current page state."""
        page = self._require_page()

        title = await page.title()
        url = page.url

        # Get accessibility tree from Playwright
        tree_dict = await page.accessibility.snapshot()

        if tree_dict is None:
            # Empty page or error - create minimal tree
            tree_dict = {"role": "WebArea", "name": "", "children": []}

        # Convert to our AccessibilityNode format
        tree = AccessibilityNode.from_playwright_dict(tree_dict)

        # Generate semantic snapshot
        snapshot_text, refmap = self._snapshot_generator.generate(tree, title=title, url=url)

        # Store refmap for future interactions
        self._refmap = refmap

        return NavigationResult(snapshot=snapshot_text, refmap=refmap)

    async def navigate(self, url: str) -> NavigationResult:
        """Navigate to a URL and return page snapshot.

        Args:
            url: The URL to navigate to

        Returns:
            NavigationResult with snapshot text and refmap
        """
        page = self._require_page()

        await page.goto(url, wait_until="domcontentloaded")

        return await self._take_snapshot()

    async def click(self, ref: int) -> NavigationResult:
        """Click an element by its reference number.

        Args:
            ref: The reference number from the snapshot

        Returns:
            NavigationResult with updated page state
        """
        page = self._require_page()

        selector = self._refmap.get_selector(ref)
        if selector is None:
            raise ValueError(f"Invalid ref: {ref}. Element not found in current snapshot.")

        locator = page.locator(selector)
        await locator.click()

        # Return updated snapshot
        return await self._take_snapshot()

    async def type_text(self, ref: int, text: str) -> str:
        """Type text into an element by its reference number.

        Uses fill() which replaces any existing content.

        Args:
            ref: The reference number from the snapshot
            text: The text to type

        Returns:
            Confirmation message
        """
        page = self._require_page()

        selector = self._refmap.get_selector(ref)
        if selector is None:
            raise ValueError(f"Invalid ref: {ref}. Element not found in current snapshot.")

        locator = page.locator(selector)
        await locator.fill(text)

        return f"Typed text into element [ref={ref}]"

    async def scroll(self, direction: str = "down") -> NavigationResult:
        """Scroll the page.

        Args:
            direction: "up" or "down"

        Returns:
            NavigationResult with updated page state
        """
        page = self._require_page()

        if direction not in ("up", "down"):
            raise ValueError(f"Invalid direction: {direction}. Must be 'up' or 'down'.")

        amount = self.SCROLL_AMOUNT if direction == "down" else -self.SCROLL_AMOUNT

        await page.evaluate(f"window.scrollBy(0, {amount})")

        return await self._take_snapshot()

    async def snapshot(self) -> NavigationResult:
        """Get current page snapshot without any interaction.

        Returns:
            NavigationResult with current page state
        """
        return await self._take_snapshot()

    async def screenshot(self, path: str | None = None) -> str:
        """Take a screenshot of the current page.

        Args:
            path: Path to save screenshot. If None, uses default timestamped name.

        Returns:
            Path where screenshot was saved
        """
        page = self._require_page()

        if path is None:
            timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = f"screenshot_{timestamp}.png"

        # Ensure path is absolute
        path_obj = Path(path)
        if not path_obj.is_absolute():
            path_obj = Path.cwd() / path_obj

        await page.screenshot(path=str(path_obj))

        return str(path_obj)


__all__ = ["BrowserDriver", "NavigationResult"]
