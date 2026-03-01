# Browser automation tool for AI agent control
# Changes: Initial creation with BrowserTool class
#
# Provides browser automation capabilities through Playwright with semantic
# accessibility tree snapshots for LLM-based browser control.
"""Browser automation tool for agent use."""

from __future__ import annotations

from typing import Any

from ...browser.session import get_browser_session_manager
from ..protocol import BaseTool


class BrowserTool(BaseTool):
    """Browser automation tool using Playwright.

    Provides actions for navigating, clicking, typing, scrolling,
    and capturing screenshots. Uses semantic accessibility tree
    snapshots for element identification.

    Actions:
        navigate: Go to a URL
        click: Click an element by reference number
        type: Type text into an element
        scroll: Scroll the page up or down
        snapshot: Get current page accessibility snapshot
        screenshot: Take a screenshot
        close: Close the browser session
    """

    DEFAULT_SESSION_ID = "default"

    @property
    def name(self) -> str:
        return "browser"

    @property
    def description(self) -> str:
        return (
            "Control a web browser to navigate pages, click elements, fill forms, "
            "and capture screenshots. Uses semantic accessibility snapshots with "
            "[ref=N] markers for element identification."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The browser action to perform",
                    "enum": [
                        "navigate",
                        "click",
                        "type",
                        "scroll",
                        "snapshot",
                        "screenshot",
                        "close",
                    ],
                },
                "url": {
                    "type": "string",
                    "description": "URL to navigate to (required for 'navigate' action)",
                },
                "ref": {
                    "type": "integer",
                    "description": "Element reference number from snapshot (required for 'click' and 'type' actions)",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type (required for 'type' action)",
                },
                "direction": {
                    "type": "string",
                    "description": "Scroll direction: 'up' or 'down' (default: 'down')",
                    "enum": ["up", "down"],
                },
                "session_id": {
                    "type": "string",
                    "description": "Browser session ID (optional, uses default if not specified)",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **params: Any) -> str:
        """Execute a browser action.

        Args:
            action: The action to perform
            url: URL for navigate action
            ref: Element reference for click/type actions
            text: Text for type action
            direction: Direction for scroll action
            session_id: Optional session identifier

        Returns:
            Action result (snapshot, file path, or confirmation message)
        """
        action = params.get("action")
        session_id = params.get("session_id", self.DEFAULT_SESSION_ID)

        try:
            if action == "navigate":
                return await self._navigate(params, session_id)
            elif action == "click":
                return await self._click(params, session_id)
            elif action == "type":
                return await self._type(params, session_id)
            elif action == "scroll":
                return await self._scroll(params, session_id)
            elif action == "snapshot":
                return await self._snapshot(session_id)
            elif action == "screenshot":
                return await self._screenshot(session_id)
            elif action == "close":
                return await self._close(session_id)
            else:
                return self._error(f"Unknown action: {action}")

        except Exception as e:
            return self._error(str(e))

    async def _get_driver(self, session_id: str):
        """Get or create browser driver for session."""
        manager = get_browser_session_manager()
        session = await manager.get_or_create(session_id, headless=True)
        return session.driver

    async def _navigate(self, params: dict, session_id: str) -> str:
        """Handle navigate action."""
        url = params.get("url")
        if not url:
            return self._error("URL is required for navigate action")

        driver = await self._get_driver(session_id)
        result = await driver.navigate(url)
        return result.snapshot

    async def _click(self, params: dict, session_id: str) -> str:
        """Handle click action."""
        ref = params.get("ref")
        if ref is None:
            return self._error("ref is required for click action")

        driver = await self._get_driver(session_id)
        result = await driver.click(ref=int(ref))
        return result.snapshot

    async def _type(self, params: dict, session_id: str) -> str:
        """Handle type action."""
        ref = params.get("ref")
        text = params.get("text")

        if ref is None:
            return self._error("ref is required for type action")
        if text is None:
            return self._error("text is required for type action")

        driver = await self._get_driver(session_id)
        return await driver.type_text(ref=int(ref), text=text)

    async def _scroll(self, params: dict, session_id: str) -> str:
        """Handle scroll action."""
        direction = params.get("direction", "down")

        driver = await self._get_driver(session_id)
        result = await driver.scroll(direction=direction)
        return result.snapshot

    async def _snapshot(self, session_id: str) -> str:
        """Handle snapshot action."""
        driver = await self._get_driver(session_id)
        result = await driver.snapshot()
        return result.snapshot

    async def _screenshot(self, session_id: str) -> str:
        """Handle screenshot action."""
        driver = await self._get_driver(session_id)
        path = await driver.screenshot()
        return f"Screenshot saved to: {path}"

    async def _close(self, session_id: str) -> str:
        """Handle close action."""
        manager = get_browser_session_manager()
        await manager.close_session(session_id)
        return f"Browser session '{session_id}' closed"


__all__ = ["BrowserTool"]
