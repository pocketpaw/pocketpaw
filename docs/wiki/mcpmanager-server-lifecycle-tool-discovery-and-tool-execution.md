---
{
  "title": "MCPManager — Server Lifecycle, Tool Discovery, and Tool Execution",
  "summary": "`MCPManager` is the central singleton that owns the full lifecycle of connected MCP servers: it starts and stops subprocess or HTTP connections, discovers tools from each server, and dispatches tool call requests. It supports stdio, SSE, and streamable-HTTP transports as well as OAuth-authenticated remote servers.",
  "concepts": [
    "MCPManager",
    "singleton",
    "stdio transport",
    "SSE",
    "streamable-HTTP",
    "OAuth",
    "tool discovery",
    "tool caching",
    "ExceptionGroup",
    "WebSocket broadcast",
    "MCPToolInfo",
    "_ServerState"
  ],
  "categories": [
    "MCP Integration",
    "Agent Infrastructure"
  ],
  "source_docs": [
    "cf1191ae310c9c3a"
  ],
  "backlinks": null,
  "word_count": 417,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MCPManager` exists because MCP servers are external processes with complex lifecycles. Starting a stdio server means spawning a subprocess and managing its stdin/stdout pipes. Connecting to a remote server may require OAuth. Discovering tools requires an async handshake. All of this happens at application startup, so errors must be isolated per-server: one broken MCP server must not prevent the others from loading.

## Singleton Pattern

`get_mcp_manager()` returns a process-wide singleton. This is intentional: tool discovery is expensive (network round-trips), and the discovered tool list is shared by all agent instances in the process. The `_reset()` method exists for testing.

## Server Connection Strategies

The manager selects a connection strategy based on `MCPServerConfig.transport`:

| Transport | Connection Path |
|-----------|----------------|
| `stdio` | `_connect_stdio` — spawns subprocess, wraps in `StdioServerParameters` |
| `http` (SSE) | `_connect_sse` — HTTP+SSE connection, optional OAuth |
| `streamable-http` | `_connect_streamable_http` — streaming HTTP, optional OAuth |

Each connection path is wrapped in `_connect_remote_with_timeout` for HTTP transports, which enforces the configured `timeout` seconds. Without this guard, a hung remote server would block startup indefinitely.

## OAuth Flow

For OAuth-authenticated servers, `_make_oauth_auth` creates an auth context that persists tokens via `MCPTokenStorage`, handles the browser redirect via `redirect_handler` (opens a local callback URL), and resolves a `Future` when `set_oauth_callback_result` is called by the dashboard's OAuth callback endpoint.

`_handle_token_response_compat` patches over a version skew between old and new MCP SDK token response shapes, preventing crashes when the SDK is updated.

## Tool Discovery and Caching

After connecting, `_discover_tools` calls `session.list_tools()` and stores the results in `_ServerState`. Tools are indexed by server name and tool name. The cached list is returned by `discover_tools()` and `get_all_tools()` without making a network call, so agents can query available tools synchronously during routing.

## Tool Execution

`call_tool(server_name, tool_name, arguments)` looks up the server's session and calls `session.call_tool()`. Results are coerced to a string for uniform handling by agent backends. Errors are unwrapped from `ExceptionGroup` (Python 3.11+) by `_extract_root_error` so the agent sees a useful message, not an opaque group.

## WebSocket Broadcast Integration

`set_ws_broadcast(fn)` registers a callback for pushing server connection events to the dashboard UI. This decouples the MCP layer from the WebSocket transport.

## Known Gaps

No automatic reconnection: if a stdio server process dies, it is not restarted and the agent will receive errors until the manager is restarted. Tool list is not refreshed after initial discovery. OAuth state is an in-memory `Future`; if the process restarts during the OAuth flow, the user must start over.