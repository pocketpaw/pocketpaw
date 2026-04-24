---
{
  "title": "Headless Integration Tests: Server Startup, Tool Bridge, and Execution Timeouts",
  "summary": "This integration test module verifies the full headless stack from FastAPI router mounting through tool bridge availability across all agent backends, enforcing that memory tools are never excluded, that `bypassPermissions` is set unconditionally, and that individual tool calls complete within a 5-second timeout that catches the historic permission hang bug.",
  "concepts": [
    "headless integration",
    "FastAPI router mounting",
    "tool bridge",
    "memory tools",
    "bypassPermissions",
    "execution timeout",
    "asyncio.wait_for",
    "tool profiles",
    "policy deny list",
    "permission hang bug",
    "ClaudeSDKBackend",
    "concurrent tool calls"
  ],
  "categories": [
    "testing",
    "integration tests",
    "headless channels",
    "tool bridge",
    "security",
    "test"
  ],
  "source_docs": [
    "9884c0a8f4363ee1"
  ],
  "backlinks": null,
  "word_count": 500,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_integration_headless.py` is the highest-level integration test file in the test suite. It verifies that the server boots correctly, that all critical API routers mount without error, and that the tool bridge pipeline delivers the right tools to every agent backend. It also enforces strict execution timeouts to catch any recurrence of the permission hang bug.

## `TestServerStartup`

These tests use a minimal FastAPI test client rather than a full server process:

- **`test_health_router_mounts_without_error`** — the health router must mount without raising any import or configuration error.
- **`test_health_endpoint_returns_200`** — `GET /health` must return HTTP 200.
- **`test_version_endpoint_returns_package_version`** — the version endpoint must return the installed package version string.
- **`test_all_critical_v1_routers_mount_without_error`** — a sweep test that attempts to mount all v1 routers (mcp, identity, memory, tools, etc.). Any router that fails to mount would produce a silent 404 or 500 in production.
- **`test_full_dashboard_app_health_endpoint`** — an async test that exercises the dashboard app's health endpoint end-to-end.

## `TestToolBridgeCompleteness`

This class verifies that memory tools (`remember`, `recall`, `forget`) are available for every supported agent backend:

- **`test_memory_tools_present_for_backend`** — parametrized across all backends (claude_sdk, openai_agents, google_adk, subprocess). Each backend's tool bridge must include all three memory tool names.
- **`test_memory_tools_not_in_always_excluded`** — the always-excluded tool list must not contain memory tools. Memory is a core capability that no policy should block by default.
- **`test_memory_tools_not_in_claude_sdk_excluded`** — the Claude SDK backend has its own exclusion list (for shell tools); memory tools must not appear there.
- **`test_shell_tools_excluded_only_for_claude_sdk`** — shell-level tools that would conflict with the SDK's own execution model are excluded only for the Claude SDK backend.

## `TestHeadlessChannelToolAccess`

A direct regression guard against the permission hang bug:

- **`test_permission_mode_is_unconditional_in_run_source`** — uses `inspect.getsource` on `ClaudeSDKBackend.run` to assert `"bypassPermissions"` is present and `"if self.settings.bypass_permissions"` is absent.
- **`test_bypass_permissions_false_does_not_gate_permission_mode`** — constructs a backend with `bypass_permissions=False` and confirms bypassPermissions is still set.
- **`test_memory_tools_allowed_under_all_profiles`** — parametrized across tool profiles (`full`, `minimal`, `custom`). Memory tools must be accessible under every profile.
- **`test_tool_policy_deny_list_can_block_memory_tools`** — confirms that explicit deny-listing of a memory tool name does remove it. Policy overrides must work.
- **`test_group_memory_in_deny_blocks_all_memory_tools`** — deny-listing the group alias `memory` must block all memory tools at once.

## `TestToolExecutionTimeout`

Each test runs a tool call and asserts it completes within 5 seconds using `asyncio.wait_for`. The 5-second limit is generous enough to accommodate slow CI environments while still catching the infinite hang that manifested under the old broken permission mode:

- `test_remember_tool_completes_within_timeout`
- `test_recall_tool_completes_within_timeout`
- `test_forget_tool_completes_within_timeout`
- `test_remember_recall_roundtrip_within_timeout`
- `test_concurrent_tool_calls_complete_within_timeout` — runs remember, recall, and forget concurrently with `asyncio.gather` inside the timeout.

## `TestToolBridgePipelineIntegration`

End-to-end pipeline tests: policy evaluation → tool registry → tool list. Verifies that all tools in the `full` profile can be instantiated without errors and that the policy filter correctly applies deny lists.

## Known Gaps

No tests cover the case where `asyncio.wait_for` itself times out in CI due to external resource contention (the test would then flake rather than fail cleanly). No tests verify tool bridge behavior under the Google ADK or OpenRouter backends specifically.