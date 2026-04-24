---
{
  "title": "MCP Manager: Server Lifecycle, Tool Discovery, Config Persistence, and HTTP Transport Auto-Detection",
  "summary": "The MCP Manager test suite (Sprint 16) covers the full lifecycle of MCP server connections — config serialization, parallel server startup, tool discovery, tool invocation, graceful shutdown, and the Streamable-HTTP-to-SSE fallback protocol negotiation. All MCP SDK imports are mocked as the `mcp` package is optional.",
  "concepts": [
    "MCPManager",
    "MCP server",
    "config persistence",
    "parallel startup",
    "tool discovery",
    "tool invocation",
    "HTTP transport",
    "SSE fallback",
    "Streamable HTTP",
    "singleton",
    "connection state machine",
    "MCPServerConfig"
  ],
  "categories": [
    "MCP integration",
    "server management",
    "test"
  ],
  "source_docs": [
    "0d8e3a67bbf24681"
  ],
  "backlinks": null,
  "word_count": 485,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `MCPManager` is PocketPaw's runtime for managing connections to external MCP servers. It handles config persistence (load/save from JSON), connection state tracking, tool discovery, and RPC-style tool invocation. Because `mcp` is an optional dependency, the entire SDK surface is mocked in tests using `SimpleNamespace` and `AsyncMock`.

## Config Serialization

`TestMCPServerConfig` validates the `MCPServerConfig` dataclass:

- `to_dict()` / `from_dict()` roundtrip preserves all fields including `timeout` and `env`.
- `from_dict({})` returns a valid object with default values rather than raising — defensive against malformed config files.
- `from_dict` with a missing `name` key yields an empty string rather than `KeyError`.

## Config File Persistence

`TestMCPConfig` tests `load_mcp_config()` and `save_mcp_config()` against a temp directory:

- **No file**: returns an empty list (first-run case).
- **Corrupt JSON**: returns an empty list rather than crashing — a corrupt config file should not prevent the app from starting.
- **Missing `servers` key**: same graceful fallback.
- **Save creates file**: the config directory and file are created if absent.

## Parallel Server Startup

`test_start_enabled_servers_parallel` verifies that when multiple servers are configured and enabled, `MCPManager.start_enabled_servers()` starts them concurrently rather than sequentially. This reduces startup latency proportionally to the number of servers.

## Connection State Machine

- `test_start_server_already_connected` confirms that calling `start_server()` on an already-connected server is a no-op.
- `test_stop_server_not_running` confirms that stopping a server that was never started returns gracefully.
- `test_stop_server_running` confirms that a running server's connection is closed and its state is removed.

## Tool Discovery and Invocation

`test_discover_tools_unknown_server` verifies that requesting tools from an unconfigured server returns an empty list rather than raising. `test_call_tool_success` mocks a successful RPC response; `test_call_tool_error` and `test_call_tool_no_text` cover error and empty-result scenarios respectively — both must return a result dict rather than raising, so the agent loop can surface the error to the user.

## HTTP Transport Auto-Detection

`TestHTTPAutoDetect` covers the Streamable HTTP → SSE fallback:

- `test_http_transport_tries_streamable_first`: the manager tries the newer Streamable HTTP protocol first.
- `test_http_transport_falls_back_to_sse`: if Streamable HTTP fails with a connection error, the manager retries with SSE.
- `test_http_transport_no_fallback_on_timeout`: a timeout error does not trigger the SSE fallback — only connection-refused errors do. This prevents the manager from silently switching protocols when the server is simply slow.

This negotiation exists because MCP's HTTP transport evolved from SSE-based to Streamable HTTP, and many servers in the wild support only one or the other.

## Singleton Pattern

`TestGetMCPManager` confirms that `get_mcp_manager()` returns the same instance on repeated calls — a singleton pattern that prevents multiple manager instances from competing for the same server connections.

## Config CRUD

`TestMCPManagerConfigMethods` tests `add_server_config`, `remove_server_config`, and `toggle_server_config` through the manager facade, including the replace-on-duplicate behavior for `add`.

## Known Gaps

- `test_start_server_stdio_success` uses a `patched_connect` inner async method defined inside the test class — this coupling to internal connection logic makes the test brittle if the connect interface changes.
- There are no tests for reconnection after a server crash (dropped connection mid-session).