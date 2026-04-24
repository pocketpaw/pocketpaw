---
{
  "title": "MCP Router Tests: Server Management and OAuth Callback Handling",
  "summary": "This test file covers PocketPaw's `/api/v1/mcp` router, which manages Model Context Protocol (MCP) server connections — add, remove, toggle, and status — and handles the OAuth callback that completes browser-based MCP server authentication flows.",
  "concepts": [
    "MCP",
    "Model Context Protocol",
    "MCPManager",
    "server management",
    "OAuth callback",
    "state token",
    "add server",
    "remove server",
    "toggle server",
    "stdio transport",
    "get_mcp_manager"
  ],
  "categories": [
    "MCP",
    "API",
    "testing",
    "OAuth",
    "test"
  ],
  "source_docs": [
    "b2578c628a5d777f"
  ],
  "backlinks": null,
  "word_count": 441,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw can connect to external MCP servers that extend the agent's tool set. The MCP router exposes a management API for these connections, handled by `get_mcp_manager()` which returns a singleton `MCPManager`. The OAuth callback endpoint is the redirect target for browser-based auth flows that some MCP servers require.

## Status (`GET /mcp/status`)

`TestMCPStatus` verifies that `manager.get_server_status()` is called and its result is returned directly. The response is a dict keyed by server name, with each value containing connection metadata. The test confirms a known server name (`"test-server"`) appears in the response, exercising the serialisation path.

## Add Server (`POST /mcp/add`)

`TestMCPAdd` covers two cases:

- **Success**: A valid payload with `name`, `transport`, `command`, and `args` calls `mgr.add_server_config` and then `mgr.start_server`. The test uses `assert_called_once()` to confirm the config is saved before the server is started — if the order were reversed, a startup failure would leave no persistent config.
- **Missing name**: An empty `name` field returns 400 (not a Pydantic 422), indicating the router has an explicit validation check beyond what Pydantic enforces. This distinction matters because an empty string passes Pydantic's `str` type check but is semantically invalid as a server identifier.

## Remove Server (`POST /mcp/remove`)

- **Existing server**: Calls `stop_server` (to cleanly disconnect) and `remove_server_config`. Returns `{"status": "ok"}`.
- **Non-existent server**: `remove_server_config` returns `False`; response is 200 with an `error` field. Returning 200 rather than 404 reflects the idempotent intent — the end state (server not configured) is achieved regardless.

## Toggle (`POST /mcp/toggle`)

Attempting to toggle a server that is not in `get_server_status()` returns 200 with an `error` field. Like remove, this is a soft failure — the client is informed without triggering an HTTP error code that might be mishandled.

## OAuth Callback (`GET /mcp/oauth/callback`)

MCP servers that use OAuth redirect the browser to this endpoint after authentication:

```python
GET /api/v1/mcp/oauth/callback?code=abc&state=xyz
```

Three cases:

- **Missing parameters**: No `code` or `state` query params returns 400. Without these, the callback cannot complete the OAuth exchange.
- **Success**: `set_oauth_callback_result` returns `True`; the response is a 200 HTML page containing "Authenticated". The HTML format is deliberate — the browser is displaying this page to the user, not a headless client parsing JSON.
- **Expired callback**: `set_oauth_callback_result` returns `False` (the state token has expired or was never registered). Returns 400. This prevents an old redirect URL from being replayed after the state window has closed.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover: enabling a server that is currently disabled (the toggle success path), what happens if `start_server` raises during add, or concurrent add/remove operations for the same server name.