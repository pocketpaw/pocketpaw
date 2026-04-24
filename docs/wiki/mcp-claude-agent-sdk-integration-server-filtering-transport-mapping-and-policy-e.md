---
{
  "title": "MCP + Claude Agent SDK Integration: Server Filtering, Transport Mapping, and Policy Enforcement",
  "summary": "The MCP-Claude SDK integration tests (Sprint 17) validate that `ClaudeAgentSDK._get_mcp_servers()` correctly translates PocketPaw's internal `MCPServerConfig` objects into the format expected by the Claude Agent SDK, applying enable/disable filtering, transport type mapping, and security policy checks before handing the server list to the SDK.",
  "concepts": [
    "MCP",
    "Claude Agent SDK",
    "MCPServerConfig",
    "_get_mcp_servers",
    "transport type",
    "stdio",
    "http",
    "sse",
    "security policy",
    "enable/disable filtering",
    "environment variables",
    "import error resilience"
  ],
  "categories": [
    "MCP integration",
    "agent runtime",
    "test"
  ],
  "source_docs": [
    "786cf5d3e8298f5a"
  ],
  "backlinks": null,
  "word_count": 464,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw embeds the Claude Agent SDK to power its AI agent loop. The SDK requires MCP (Model Context Protocol) servers to be declared at agent initialization time in a specific dict format. `_get_mcp_servers()` bridges the gap: it reads PocketPaw's persisted `MCPServerConfig` list and transforms it into the SDK's expected structure. The test suite validates every filtering and mapping rule.

## Test Infrastructure

All SDK imports are mocked — `ClaudeAgentSDK._initialize` is patched during construction and `_sdk_available` is set to `False`. This allows tests to run without the `anthropic` SDK installed and without network access. The `_strip_builtin_servers()` helper removes always-on in-process MCP servers from the result before assertions, keeping tests focused on the external-config logic.

## Enable/Disable Filtering

`test_enabled_stdio_server_passes` confirms that an enabled stdio server appears in the result. `test_disabled_server_filtered_out` confirms that a server with `enabled=False` is excluded. This filtering prevents the SDK from attempting to connect to servers that the user has toggled off in the dashboard.

## Transport Type Mapping

The SDK distinguishes three transport types:

- **stdio**: `command` + `args` are passed directly.
- **http**: `url` is required; `test_http_server_without_url_skipped` verifies that an http server with an empty URL is excluded (an empty URL would cause the SDK to fail at connection time with an unhelpful error).
- **sse**: `url` is passed as-is; `test_sse_server_passes` confirms this path.

## Security Policy Enforcement

`test_policy_denies_server` and `test_policy_denies_group_mcp` test that a security policy layer can block specific servers or entire MCP groups before they reach the SDK. This is the mechanism for enterprise deployments that need to restrict which external tools agents can access.

## Environment Variable Passthrough

`test_env_passed_through` confirms that `MCPServerConfig.env` (a dict of environment variable overrides) is included in the SDK server spec. `test_empty_env_and_args_omitted` verifies that empty `env` and `args` are excluded from the output dict rather than being passed as empty collections — the SDK may treat the presence of an empty `args` list differently from its absence.

## Import Error Resilience

`test_mcp_import_error_returns_empty` simulates a scenario where the `mcp` package is not installed by making the import raise `ImportError`. In this case, `_get_mcp_servers()` must return an empty dict rather than propagating the error. This allows PocketPaw to function as a basic chat assistant even in environments where MCP is not available.

## Mixed Server Test

`test_multiple_servers_mixed` combines enabled, disabled, stdio, and http servers in one config list and asserts the exact set of servers that should appear in the output, validating the combined behavior of all filters.

## Known Gaps

- Policy enforcement tests use a mock policy object; the actual policy evaluation engine (rules engine, ACL list) is not exercised here.
- There is no test for what happens when `_get_mcp_servers()` is called after the SDK is already initialized with a different server list — the SDK may not support live reconfiguration.