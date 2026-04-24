---
{
  "title": "Google ADK Backend — Gemini Agent Integration via Google Agent Development Kit",
  "summary": "Implements `GoogleADKBackend`, which runs Gemini-powered agents using the official `google-adk` Python SDK. It supports built-in tools (Google Search, code execution), MCP toolset integration via both stdio and SSE transports, custom PocketPaw function tools, and streaming via the ADK's `InMemoryRunner`.",
  "concepts": [
    "GoogleADKBackend",
    "google-adk",
    "InMemoryRunner",
    "Gemini",
    "MCP toolsets",
    "FunctionTool",
    "google_search",
    "code_execution",
    "session management",
    "streaming events",
    "ToolPolicy"
  ],
  "categories": [
    "agent-runtime",
    "google",
    "gemini",
    "mcp"
  ],
  "source_docs": [
    "66e51de37d08c961"
  ],
  "backlinks": null,
  "word_count": 459,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`GoogleADKBackend` provides native Gemini model support through Google's Agent Development Kit (ADK). Unlike subprocess-based backends, ADK runs entirely in-process as a Python SDK, giving tighter integration with session management, tool registration, and streaming.

## InMemoryRunner and Session Management

The ADK's `InMemoryRunner` manages conversation sessions identified by `app_name` + `user_id` + `session_id`. PocketPaw maps its `session_key` to an ADK session ID, so multi-turn conversations maintain context across `run()` calls. The runner is created once per `(instruction, tools)` combination and reused.

## Built-in Tools

`google_search` and `code_execution` are registered as ADK built-in tools. Both map to PocketPaw's security policy categories (`browser` and `shell` respectively) via `tool_policy_map`, ensuring the security layer applies the correct trust checks regardless of which backend is active.

## MCP Toolset Integration

`_build_mcp_toolsets()` constructs ADK `MCPToolset` objects from PocketPaw's MCP server config. ADK supports both stdio (local process) and SSE (HTTP) MCP transports natively. Unlike backends that inject MCP as a post-hoc layer, ADK treats MCP toolsets as first-class participants in its planning loop.

## Custom Tool Bridge

`_build_custom_tools()` wraps PocketPaw tools as ADK `FunctionTool` objects. The `GOOGLE_API_KEY` env var is read at runtime (not at import) to support late configuration. If the key is absent, the backend raises a clear error rather than failing silently during the first tool call.

## History Injection

ADK's `InMemoryRunner` supports session history natively via session IDs, but `_inject_history()` also prepends prior turns into the instruction for the initial message. This dual approach handles the case where the runner is recreated (e.g., after an instruction change) but history should still be available.

## Event Stream Translation

`run()` iterates over ADK's streaming events and translates each to the corresponding `AgentEvent`. ADK emits `ModelTextChunk`, `FunctionCallEvent`, `FunctionResponseEvent`, and `FinalResponse` events. PocketPaw maps these to `TextChunk`, `ToolUse`, `ToolResult`, and `Done` respectively.

## Known Gaps

- `InMemoryRunner` does not persist sessions across process restarts. Long-running deployments lose conversation context on restart.
- MCP toolset teardown is not explicitly called; ADK manages lifecycle internally but this may leave open SSE connections.
- `GOOGLE_API_KEY` is the only auth mechanism; service account credentials are not supported.


## Tool Policy Enforcement

PocketPaw's `ToolPolicy` checks apply to ADK tool calls via `tool_policy_map`. When an ADK agent calls `google_search`, the security layer checks the `browser` policy. When it calls `code_execution`, the `shell` policy applies. This means ADK tool calls receive the same trust-level enforcement as any other backend, despite being routed through Google's SDK rather than PocketPaw's own tool dispatcher.

## Cleanup and Resource Management

`stop()` signals the backend to halt. The `InMemoryRunner` holds per-session state; calling `stop()` clears the runner cache. ADK MCP toolsets that use SSE connections may leave open HTTP sessions if not explicitly torn down — this is a known limitation of the current ADK SDK version.
