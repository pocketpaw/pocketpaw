---
{
  "title": "Claude Agent SDK Backend — Recommended Default Agent Runtime",
  "summary": "Implements `ClaudeSDKBackend`, PocketPaw's primary agent backend that wraps Anthropic's `claude-agent-sdk`. It provides built-in tools (Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch), streaming token delivery, a `PreToolUse` security hook, MCP server support, and headless permission bypass for messaging channels.",
  "concepts": [
    "ClaudeSDKBackend",
    "PreToolUse hook",
    "headless permission bypass",
    "ToolPolicy",
    "session reuse",
    "MCP server",
    "streaming",
    "security rails",
    "is_substring_blocked",
    "AgentEvent",
    "tool policy map"
  ],
  "categories": [
    "agent-runtime",
    "security",
    "streaming",
    "claude"
  ],
  "source_docs": [
    "03920efdd87d2467"
  ],
  "backlinks": null,
  "word_count": 429,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ClaudeSDKBackend` is the recommended default backend for PocketPaw. It delegates agent execution to the official `claude-agent-sdk` package, which runs Claude models with full tool access. The backend wraps the SDK in PocketPaw's `AgentBackend` protocol so it can be swapped for any other backend without changing the calling code.

## Headless Permission Bypass

A critical design decision made on 2026-03-11: when running in headless mode (no attached terminal), the backend always sets `allow_all_permissions=True`. Without this, tool calls — especially Bash commands used for memory saves — hang indefinitely on messaging channels (Telegram, Discord, Slack) because the SDK tries to display a permission prompt to a terminal that does not exist. This is not a security downgrade: PocketPaw's `PreToolUse` hook (`_block_dangerous_hook`) enforces all security policies before any tool executes.

## Security Hook — `_block_dangerous_hook`

Every tool invocation passes through `_block_dangerous_hook` before the SDK executes it. The hook:
1. Maps the SDK tool name (e.g., `"Bash"`) to a PocketPaw policy category (e.g., `"shell"`) via `_TOOL_POLICY_MAP`.
2. Calls `ToolPolicy.check()` to verify the agent's trust level permits the category.
3. Calls `is_substring_blocked()` to scan the tool input for blocked command patterns (e.g., `rm -rf /`, credential exfiltration).
4. Returns a `block` decision if either check fails; otherwise passes through.

This two-layer check catches both policy violations (an agent that shouldn't use Bash at all) and content violations (a permitted agent submitting a dangerous command).

## Session Reuse and the Client Cache

`_get_or_create_client()` maintains a dict keyed by `session_key`. Reusing the same SDK client for a conversation preserves the model's conversation context across multiple `run()` calls. The cache is scoped to the backend instance and cleared by `cleanup()`.

## Resilient Execution

`_resilient_query()` and `_resilient_receive()` add retry logic around the SDK's network calls. The SDK can return connection errors or rate-limit responses; rather than surfacing these as hard failures, the backend retries with exponential back-off before emitting an error event.

## MCP Server Integration

The backend accepts a list of MCP server configs and passes them to the SDK client. Each MCP server is validated: HTTP/SSE transports must use HTTPS or localhost; stdio transports validate the command path. This prevents SSRF via rogue MCP server URLs.

## Streaming Event Translation

The SDK yields its own message objects; `run()` translates each into the canonical `AgentEvent` variants (`TextChunk`, `ToolUse`, `ToolResult`, `Done`, `Error`) so the `AgentLoop` never imports SDK-specific types.

## Known Gaps

- Session cleanup on abnormal disconnect is best-effort; long-lived server processes may accumulate stale client objects until `cleanup()` is called.
- `_on_stderr()` logs stderr from subprocesses but does not surface them as structured events.
