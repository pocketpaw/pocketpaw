# Browser driver tests
# Changes: Initial creation with comprehensive Playwright driver tests
# Fixed mocking for async_playwright().start() pattern
"""Tests for browser driver module."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pocketclaw.browser.driver import BrowserDriver
from pocketclaw.browser.snapshot import RefMap


class TestBrowserDriverInit:
    """Tests for BrowserDriver initialization."""

    def test_driver_init(self):
        """Should initialize with headless mode by default."""
        driver = BrowserDriver()
        assert driver.headless is True
        assert driver._browser is None
        assert driver._page is None

    def test_driver_init_headful(self):
        """Should support headful mode."""
        driver = BrowserDriver(headless=False)
        assert driver.headless is False


class TestBrowserDriverContextManager:
    """Tests for async context manager support."""

    @pytest.mark.asyncio
    async def test_context_manager_launches_browser(self):
        """Should launch browser on enter and close on exit."""
        driver = BrowserDriver()

        with (
            patch.object(driver, "launch", new_callable=AsyncMock) as mock_launch,
            patch.object(driver, "close", new_callable=AsyncMock) as mock_close,
        ):
            async with driver:
                mock_launch.assert_called_once()
            mock_close.assert_called_once()


class TestBrowserDriverLaunch:
    """Tests for browser launch functionality."""

    @pytest.mark.asyncio
    async def test_launch_creates_browser_and_page(self):
        """Should create browser and page on launch."""
        driver = BrowserDriver()

        # Mock playwright - it uses async_playwright().start()
        mock_browser = AsyncMock()
        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        # async_playwright() returns something with start() method
        mock_pw_cm = MagicMock()
        mock_pw_cm.start = AsyncMock(return_value=mock_playwright)

        with patch("playwright.async_api.async_playwright", return_value=mock_pw_cm):
            await driver.launch()

            assert driver._browser is not None
            assert driver._page is not None

    @pytest.mark.asyncio
    async def test_launch_sets_viewport(self):
        """Should set reasonable viewport size."""
        driver = BrowserDriver()

        mock_browser = AsyncMock()
        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

        mock_pw_cm = MagicMock()
        mock_pw_cm.start = AsyncMock(return_value=mock_playwright)

        with patch("playwright.async_api.async_playwright", return_value=mock_pw_cm):
            await driver.launch()

            # Should call new_context with viewport
            mock_browser.new_context.assert_called_once()
            call_kwargs = mock_browser.new_context.call_args.kwargs
            assert "viewport" in call_kwargs


class TestBrowserDriverNavigation:
    """Tests for navigation functionality."""

    @pytest.mark.asyncio
    async def test_navigate_goes_to_url(self):
        """Should navigate to the given URL."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._page.goto = AsyncMock()
        driver._page.title = AsyncMock(return_value="Test Page")
        driver._page.url = "https://example.com"
        driver._page.accessibility.snapshot = AsyncMock(
            return_value={"role": "WebArea", "name": "Test Page", "children": []}
        )

        result = await driver.navigate("https://example.com")

        driver._page.goto.assert_called_once_with(
            "https://example.com", wait_until="domcontentloaded"
        )
        assert "Page: Test Page" in result.snapshot

    @pytest.mark.asyncio
    async def test_navigate_returns_snapshot_and_refmap(self):
        """Should return snapshot text and refmap."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._page.goto = AsyncMock()
        driver._page.title = AsyncMock(return_value="Example")
        driver._page.url = "https://example.com"
        driver._page.accessibility.snapshot = AsyncMock(
            return_value={
                "role": "WebArea",
                "name": "Example",
                "children": [{"role": "button", "name": "Click Me"}],
            }
        )

        result = await driver.navigate("https://example.com")

        assert isinstance(result.snapshot, str)
        assert isinstance(result.refmap, RefMap)
        assert "[ref=1]" in result.snapshot

    @pytest.mark.asyncio
    async def test_navigate_requires_page(self):
        """Should raise error if browser not launched."""
        driver = BrowserDriver()

        with pytest.raises(RuntimeError, match="Browser not launched"):
            await driver.navigate("https://example.com")


class TestBrowserDriverClick:
    """Tests for click functionality."""

    @pytest.mark.asyncio
    async def test_click_by_ref(self):
        """Should click element by reference number."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._refmap = RefMap()
        driver._refmap.add('role=button[name="Submit"]')

        driver._page.locator = MagicMock()
        mock_locator = AsyncMock()
        driver._page.locator.return_value = mock_locator
        driver._page.title = AsyncMock(return_value="Form")
        driver._page.url = "https://example.com/form"
        driver._page.accessibility.snapshot = AsyncMock(
            return_value={"role": "WebArea", "name": "Form", "children": []}
        )

        result = await driver.click(ref=1)

        driver._page.locator.assert_called_once_with('role=button[name="Submit"]')
        mock_locator.click.assert_called_once()
        assert result.snapshot is not None

    @pytest.mark.asyncio
    async def test_click_invalid_ref(self):
        """Should raise error for invalid ref."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._refmap = RefMap()

        with pytest.raises(ValueError, match="Invalid ref"):
            await driver.click(ref=999)

    @pytest.mark.asyncio
    async def test_click_requires_page(self):
        """Should raise error if browser not launched."""
        driver = BrowserDriver()

        with pytest.raises(RuntimeError, match="Browser not launched"):
            await driver.click(ref=1)


class TestBrowserDriverType:
    """Tests for text input functionality."""

    @pytest.mark.asyncio
    async def test_type_text(self):
        """Should type text into element by ref."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._refmap = RefMap()
        driver._refmap.add('role=textbox[name="Username"]')

        driver._page.locator = MagicMock()
        mock_locator = AsyncMock()
        driver._page.locator.return_value = mock_locator

        await driver.type_text(ref=1, text="testuser")

        driver._page.locator.assert_called_once_with('role=textbox[name="Username"]')
        mock_locator.fill.assert_called_once_with("testuser")

    @pytest.mark.asyncio
    async def test_type_clears_field_first(self):
        """Should clear field before typing (fill behavior)."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._refmap = RefMap()
        driver._refmap.add('role=textbox[name="Email"]')

        driver._page.locator = MagicMock()
        mock_locator = AsyncMock()
        driver._page.locator.return_value = mock_locator

        await driver.type_text(ref=1, text="new@email.com")

        # fill() replaces content (clears first)
        mock_locator.fill.assert_called_once_with("new@email.com")

    @pytest.mark.asyncio
    async def test_type_invalid_ref(self):
        """Should raise error for invalid ref."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._refmap = RefMap()

        with pytest.raises(ValueError, match="Invalid ref"):
            await driver.type_text(ref=999, text="hello")


class TestBrowserDriverScroll:
    """Tests for scroll functionality."""

    @pytest.mark.asyncio
    async def test_scroll_down(self):
        """Should scroll down."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._page.title = AsyncMock(return_value="Page")
        driver._page.url = "https://example.com"
        driver._page.accessibility.snapshot = AsyncMock(
            return_value={"role": "WebArea", "name": "Page", "children": []}
        )

        result = await driver.scroll(direction="down")

        driver._page.evaluate.assert_called()
        assert result.snapshot is not None

    @pytest.mark.asyncio
    async def test_scroll_up(self):
        """Should scroll up."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._page.title = AsyncMock(return_value="Page")
        driver._page.url = "https://example.com"
        driver._page.accessibility.snapshot = AsyncMock(
            return_value={"role": "WebArea", "name": "Page", "children": []}
        )

        await driver.scroll(direction="up")

        driver._page.evaluate.assert_called()

    @pytest.mark.asyncio
    async def test_scroll_invalid_direction(self):
        """Should raise error for invalid direction."""
        driver = BrowserDriver()
        driver._page = AsyncMock()

        with pytest.raises(ValueError, match="Invalid direction"):
            await driver.scroll(direction="sideways")


class TestBrowserDriverSnapshot:
    """Tests for snapshot functionality."""

    @pytest.mark.asyncio
    async def test_snapshot_returns_current_state(self):
        """Should return current page snapshot."""
        driver = BrowserDriver()
        driver._page = AsyncMock()
        driver._page.title = AsyncMock(return_value="Current Page")
        driver._page.url = "https://example.com/current"
        driver._page.accessibility.snapshot = AsyncMock(
            return_value={
                "role": "WebArea",
                "name": "Current Page",
                "children": [{"role": "heading", "name": "Hello", "level": 1}],
            }
        )

        result = await driver.snapshot()

        assert "Page: Current Page" in result.snapshot
        assert "URL: https://example.com/current" in result.snapshot
        assert "heading" in result.snapshot
        assert "Hello" in result.snapshot


class TestBrowserDriverScreenshot:
    """Tests for screenshot functionality."""

    @pytest.mark.asyncio
    async def test_screenshot_saves_file(self):
        """Should save screenshot to file."""
        driver = BrowserDriver()
        driver._page = AsyncMock()

        path = await driver.screenshot("/tmp/test.png")

        driver._page.screenshot.assert_called_once()
        assert path == "/tmp/test.png"

    @pytest.mark.asyncio
    async def test_screenshot_default_path(self):
        """Should use default path if not provided."""
        driver = BrowserDriver()
        driver._page = AsyncMock()

        with patch("pocketclaw.browser.driver.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = "20240101_120000"
            path = await driver.screenshot()

        assert "screenshot_20240101_120000.png" in path


class TestBrowserDriverClose:
    """Tests for browser close functionality."""

    @pytest.mark.asyncio
    async def test_close_browser(self):
        """Should close browser and cleanup."""
        driver = BrowserDriver()
        mock_browser = AsyncMock()
        driver._browser = mock_browser
        driver._page = AsyncMock()

        await driver.close()

        mock_browser.close.assert_called_once()
        assert driver._browser is None
        assert driver._page is None

    @pytest.mark.asyncio
    async def test_close_when_not_launched(self):
        """Should handle close when browser not launched."""
        driver = BrowserDriver()

        # Should not raise
        await driver.close()


class TestBrowserDriverProperties:
    """Tests for driver properties."""

    def test_is_launched(self):
        """Should report launch status."""
        driver = BrowserDriver()
        assert driver.is_launched is False

        driver._browser = MagicMock()
        driver._page = MagicMock()
        assert driver.is_launched is True

    def test_current_url(self):
        """Should return current URL."""
        driver = BrowserDriver()
        driver._page = MagicMock()
        driver._page.url = "https://example.com/test"

        assert driver.current_url == "https://example.com/test"

    def test_current_url_none_when_not_launched(self):
        """Should return None when not launched."""
        driver = BrowserDriver()
        assert driver.current_url is None
