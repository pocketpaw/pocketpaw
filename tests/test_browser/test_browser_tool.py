# Browser tool tests
# Changes: Initial creation with comprehensive tool integration tests
"""Tests for browser tool integration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.tools.builtin.browser import BrowserTool
from pocketpaw.tools.protocol import BaseTool


class TestBrowserToolDefinition:
    """Tests for BrowserTool metadata and definition."""

    def test_tool_name(self):
        """Should have correct name."""
        tool = BrowserTool()
        assert tool.name == "browser"

    def test_tool_description(self):
        """Should have meaningful description."""
        tool = BrowserTool()
        assert "browser" in tool.description.lower()
        assert len(tool.description) > 20

    def test_tool_inherits_base(self):
        """Should inherit from BaseTool."""
        tool = BrowserTool()
        assert isinstance(tool, BaseTool)

    def test_trust_level(self):
        """Should have high trust level."""
        tool = BrowserTool()
        assert tool.trust_level == "high"

    def test_parameters_schema(self):
        """Should define action and other parameters."""
        tool = BrowserTool()
        params = tool.parameters

        assert params["type"] == "object"
        assert "action" in params["properties"]
        assert "action" in params["required"]

        # Action should be an enum
        action_schema = params["properties"]["action"]
        assert "enum" in action_schema

        # Should support these actions
        actions = action_schema["enum"]
        assert "navigate" in actions
        assert "click" in actions
        assert "type" in actions
        assert "scroll" in actions
        assert "snapshot" in actions
        assert "screenshot" in actions
        assert "close" in actions

    def test_parameters_include_url(self):
        """Should include url parameter for navigate."""
        tool = BrowserTool()
        params = tool.parameters

        assert "url" in params["properties"]

    def test_parameters_include_ref(self):
        """Should include ref parameter for click/type."""
        tool = BrowserTool()
        params = tool.parameters

        assert "ref" in params["properties"]

    def test_parameters_include_text(self):
        """Should include text parameter for type action."""
        tool = BrowserTool()
        params = tool.parameters

        assert "text" in params["properties"]

    def test_definition_export(self):
        """Should export valid definition."""
        tool = BrowserTool()
        definition = tool.definition

        assert definition.name == "browser"
        assert definition.trust_level == "high"
        assert definition.parameters == tool.parameters


class TestBrowserToolNavigate:
    """Tests for navigate action."""

    @pytest.mark.asyncio
    async def test_navigate_success(self):
        """Should navigate and return snapshot."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.navigate = AsyncMock(
            return_value=MagicMock(
                snapshot='Page: Example\nURL: https://example.com\n\n- heading "Welcome"'
            )
        )

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            result = await tool.execute(action="navigate", url="https://example.com")

            assert "Page: Example" in result
            mock_driver.navigate.assert_called_once_with("https://example.com")

    @pytest.mark.asyncio
    async def test_navigate_requires_url(self):
        """Should error if URL not provided."""
        tool = BrowserTool()

        result = await tool.execute(action="navigate")

        assert "Error" in result
        assert "url" in result.lower()


class TestBrowserToolClick:
    """Tests for click action."""

    @pytest.mark.asyncio
    async def test_click_success(self):
        """Should click element by ref and return snapshot."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.click = AsyncMock(
            return_value=MagicMock(snapshot='Page: After Click\n\n- button "Clicked"')
        )

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            result = await tool.execute(action="click", ref=1)

            assert "Page: After Click" in result
            mock_driver.click.assert_called_once_with(ref=1)

    @pytest.mark.asyncio
    async def test_click_requires_ref(self):
        """Should error if ref not provided."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            result = await tool.execute(action="click")

            assert "Error" in result
            assert "ref" in result.lower()


class TestBrowserToolType:
    """Tests for type action."""

    @pytest.mark.asyncio
    async def test_type_success(self):
        """Should type text into element."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.type_text = AsyncMock(return_value="Typed text into element [ref=1]")

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            result = await tool.execute(action="type", ref=1, text="hello@example.com")

            assert "Typed" in result
            mock_driver.type_text.assert_called_once_with(ref=1, text="hello@example.com")

    @pytest.mark.asyncio
    async def test_type_requires_ref_and_text(self):
        """Should error if ref or text not provided."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            # Missing text
            result = await tool.execute(action="type", ref=1)
            assert "Error" in result

            # Missing ref
            result = await tool.execute(action="type", text="hello")
            assert "Error" in result


class TestBrowserToolScroll:
    """Tests for scroll action."""

    @pytest.mark.asyncio
    async def test_scroll_down(self):
        """Should scroll down and return snapshot."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.scroll = AsyncMock(
            return_value=MagicMock(snapshot="Page: Scrolled\n\n- content below fold")
        )

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            await tool.execute(action="scroll", direction="down")

            mock_driver.scroll.assert_called_once_with(direction="down")

    @pytest.mark.asyncio
    async def test_scroll_up(self):
        """Should scroll up."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.scroll = AsyncMock(return_value=MagicMock(snapshot="Scrolled"))

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            await tool.execute(action="scroll", direction="up")

            mock_driver.scroll.assert_called_once_with(direction="up")

    @pytest.mark.asyncio
    async def test_scroll_default_direction(self):
        """Should default to scrolling down."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.scroll = AsyncMock(return_value=MagicMock(snapshot="Scrolled"))

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            await tool.execute(action="scroll")

            mock_driver.scroll.assert_called_once_with(direction="down")


class TestBrowserToolSnapshot:
    """Tests for snapshot action."""

    @pytest.mark.asyncio
    async def test_snapshot_returns_current_state(self):
        """Should return current page snapshot."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.snapshot = AsyncMock(
            return_value=MagicMock(
                snapshot='Page: Current\nURL: https://example.com\n\n- heading "Title"'
            )
        )

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            result = await tool.execute(action="snapshot")

            assert "Page: Current" in result
            mock_driver.snapshot.assert_called_once()


class TestBrowserToolScreenshot:
    """Tests for screenshot action."""

    @pytest.mark.asyncio
    async def test_screenshot_returns_path(self):
        """Should take screenshot and return file path."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.screenshot = AsyncMock(return_value="/tmp/screenshot_123.png")

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            result = await tool.execute(action="screenshot")

            assert "/tmp/screenshot_123.png" in result
            mock_driver.screenshot.assert_called_once()


class TestBrowserToolClose:
    """Tests for close action."""

    @pytest.mark.asyncio
    async def test_close_session(self):
        """Should close the browser session."""
        tool = BrowserTool()

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.close_session = AsyncMock()
            mock_get_mgr.return_value = mock_manager

            result = await tool.execute(action="close")

            assert "closed" in result.lower()
            mock_manager.close_session.assert_called_once()


class TestBrowserToolInvalidAction:
    """Tests for invalid action handling."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        """Should return error for unknown action."""
        tool = BrowserTool()

        result = await tool.execute(action="unknown_action")

        assert "Error" in result
        assert "unknown" in result.lower() or "invalid" in result.lower()


class TestBrowserToolErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_handles_driver_errors(self):
        """Should handle and report driver errors."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.navigate = AsyncMock(side_effect=Exception("Connection failed"))

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            result = await tool.execute(action="navigate", url="https://example.com")

            assert "Error" in result
            assert "Connection failed" in result


class TestBrowserToolSessionManagement:
    """Tests for session management behavior."""

    @pytest.mark.asyncio
    async def test_uses_default_session_id(self):
        """Should use default session ID when not specified."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.snapshot = AsyncMock(return_value=MagicMock(snapshot="Page"))

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            await tool.execute(action="snapshot")

            # Should use default session ID
            call_args = mock_manager.get_or_create.call_args
            assert call_args is not None

    @pytest.mark.asyncio
    async def test_supports_custom_session_id(self):
        """Should support custom session ID."""
        tool = BrowserTool()

        mock_session = MagicMock()
        mock_driver = AsyncMock()
        mock_session.driver = mock_driver
        mock_driver.snapshot = AsyncMock(return_value=MagicMock(snapshot="Page"))

        with patch("pocketpaw.tools.builtin.browser.get_browser_session_manager") as mock_get_mgr:
            mock_manager = AsyncMock()
            mock_manager.get_or_create = AsyncMock(return_value=mock_session)
            mock_get_mgr.return_value = mock_manager

            await tool.execute(action="snapshot", session_id="custom-123")

            mock_manager.get_or_create.assert_called_once_with("custom-123", headless=True)
