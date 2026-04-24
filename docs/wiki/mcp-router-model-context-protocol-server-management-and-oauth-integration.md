---
{
  "title": "MCP Router — Model Context Protocol Server Management and OAuth Integration",
  "summary": "The MCP router manages PocketPaw's connections to external Model Context Protocol servers — adding, removing, toggling, and testing MCP server configurations at runtime. It also provides a preset catalog for one-click MCP server installation, an OAuth callback endpoint for providers that require browser-based authorization, and auto-installation of Google Workspace skills when the GWS MCP preset is activated.",
  "concepts": [
    "MCP",
    "Model Context Protocol",
    "MCP server",
    "preset catalog",
    "OAuth callback",
    "admin scope",
    "tool discovery",
    "Google Workspace",
    "fire-and-forget",
    "MCPServerConfig",
    "MCP manager",
    "transport"
  ],
  "categories": [
    "API",
    "MCP",
    "Integration"
  ],
  "source_docs": [
    "717766bd8bebf8d0"
  ],
  "backlinks": null,
  "word_count": 473,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Model Context Protocol (MCP) servers extend a PocketPaw agent's tool set by acting as external tool providers. The MCP router is the control plane for managing these connections: it handles the full lifecycle from initial configuration through runtime toggling and OAuth-based authentication flows.

## CRUD Operations on MCP Server Configs

The router provides four operations against the MCP manager singleton:

- **`GET /mcp/status`**: Returns connection state and discovered tools for every configured server.
- **`POST /mcp/add`**: Parses a `MCPServerConfig` from the request body and registers it with the manager, optionally starting it immediately.
- **`POST /mcp/remove`**: Removes a server config and stops the connection if it's running.
- **`POST /mcp/toggle`**: Idempotently starts or stops a server depending on its current state.

All operations require `admin` scope — MCP servers can introduce arbitrary tools with broad capabilities, so restricting management to administrators prevents unprivileged users from connecting potentially malicious MCP servers.

## Test Endpoint: Dry-Run Connection

`POST /mcp/test` establishes a temporary connection to an MCP server and returns the list of tools it exposes, then disconnects. This lets users validate a server configuration before committing it — catching authentication errors, bad URLs, or incompatible transport types without polluting the running server list.

## Preset Catalog

`GET /mcp/presets` returns all built-in MCP presets with an `installed` flag. Presets are curated, pre-configured server definitions (e.g., "GitHub MCP", "Google Workspace MCP") that users can install with a single API call rather than manually constructing a `MCPServerConfig`.

`POST /mcp/presets/install` accepts a preset ID and user-supplied environment variables (API keys, OAuth tokens), merges them with the preset template, and installs the server.

## Google Workspace Auto-Skill Installation

`_install_gws_skills()` is a fire-and-forget async helper called when the Google Workspace preset is installed:

```python
async def _install_gws_skills() -> None:
    """Auto-install Google Workspace CLI agent skills (fire-and-forget)."""
```

When GWS MCP is activated, the agent gains access to Gmail, Calendar, and Drive tools. The auto-skill installation ensures that the agent's skill library (prompt templates for using those tools effectively) is also provisioned without requiring a separate manual step.

## OAuth Callback Endpoint

`GET /mcp/oauth/callback` handles the browser redirect after a user completes OAuth authorization for an MCP provider. It receives the authorization code and state parameter, exchanges the code for tokens via the MCP manager, and returns an HTML success/error page to the browser.

```python
async def mcp_oauth_callback(code: str, state: str) -> HTMLResponse:
    """OAuth callback endpoint for MCP providers."""
```

The HTML response (rather than JSON) is intentional — this endpoint is the redirect target for a browser flow, so the response needs to be human-readable.

## Known Gaps

The `add` and `toggle` endpoints accept raw JSON request bodies parsed manually (`await request.json()`) rather than through a Pydantic model. This bypasses automatic validation and means malformed input produces an uncontrolled `KeyError` or `None` rather than a clean 422 validation error.