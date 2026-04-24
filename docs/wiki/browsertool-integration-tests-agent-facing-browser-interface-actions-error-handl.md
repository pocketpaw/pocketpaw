---
{
  "title": "BrowserTool Integration Tests: Agent-Facing Browser Interface, Actions, Error Handling, and Session Management",
  "summary": "This test suite validates `BrowserTool`, the agent-facing wrapper that exposes browser automation as a single tool with an action-dispatch interface. Tests cover the tool's metadata contract (name, trust level, JSON schema), all seven supported actions (navigate, click, type, scroll, snapshot, screenshot, close), input validation, error propagation, and multi-session isolation.",
  "concepts": [
    "BrowserTool",
    "BaseTool",
    "action dispatch",
    "trust level",
    "JSON schema",
    "navigate",
    "click",
    "type",
    "scroll",
    "snapshot",
    "screenshot",
    "session management",
    "BrowserSessionManager"
  ],
  "categories": [
    "browser automation",
    "testing",
    "tools",
    "agent interface",
    "test"
  ],
  "source_docs": [
    "4c49f7e7a8e77f0c"
  ],
  "backlinks": null,
  "word_count": 401,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`BrowserTool` is a PocketPaw built-in tool that gives AI agents the ability to control a web browser. Rather than exposing separate tools for each operation, it uses an action-dispatch pattern: a single `execute(action=..., **kwargs)` call routes to the correct driver method. This test file validates every action, all validation paths, and the session management behavior.

## Tool Definition Contract

```python
class TestBrowserToolDefinition:
    def test_trust_level(self):
        assert tool.trust_level == "high"

    def test_parameters_schema(self):
        actions = params["properties"]["action"]["enum"]
        assert "navigate" in actions
        assert "click" in actions
        assert "type" in actions
        assert "scroll" in actions
        assert "snapshot" in actions
        assert "screenshot" in actions
        assert "close" in actions
```

The `trust_level == "high"` assertion matters for PocketPaw's security model: browser automation can access arbitrary URLs including internal services, so it requires explicit operator approval to enable. The JSON schema enum for `action` is tested exhaustively to prevent accidental removal of supported actions during refactoring.

## Action Tests

Each action is tested via patching `get_browser_session_manager()` and injecting a mock driver:

```python
async def test_navigate_success(self):
    mock_driver.navigate = AsyncMock(
        return_value=MagicMock(snapshot='Page: Example\nURL: https://example.com')
    )
    result = await tool.execute(action="navigate", url="https://example.com")
    assert "Page: Example" in result
    mock_driver.navigate.assert_called_once_with("https://example.com")
```

Input validation tests check the defensive layer before the driver is reached:

```python
async def test_navigate_requires_url(self):
    result = await tool.execute(action="navigate")
    assert "Error" in result
    assert "url" in result.lower()
```

Without these validations, a missing `url` in a navigate call would result in an obscure driver-level exception rather than a clear error message the agent can act on.

## Error Handling

```python
class TestBrowserToolErrorHandling:
    async def test_handles_driver_errors(self):
        mock_driver.navigate = AsyncMock(side_effect=Exception("Connection failed"))
        result = await tool.execute(action="navigate", url="https://example.com")
        assert "Error" in result
        assert "Connection failed" in result
```

Driver errors (network timeouts, browser crashes, element not found) must be caught and returned as structured error strings rather than raised exceptions. An uncaught exception from a tool would terminate the agent's turn and potentially break the conversation loop.

## Session Management

```python
async def test_supports_custom_session_id(self):
    await tool.execute(action="snapshot", session_id="custom-123")
    mock_manager.get_or_create.assert_called_once_with("custom-123", headless=True)
```

The `session_id` parameter allows agents to maintain multiple independent browser sessions, useful when browsing multiple tabs or when different tasks need isolated browser state. The default session ID test confirms that omitting `session_id` produces a deterministic default rather than a new random session on every call.

## Known Gaps

No explicit test covers what happens when `get_browser_session_manager()` itself raises (e.g., Playwright not installed). There is no test for invalid `direction` values in the scroll action (e.g., `direction="sideways"`).