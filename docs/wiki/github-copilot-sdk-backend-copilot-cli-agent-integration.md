---
{
  "title": "GitHub Copilot SDK Backend — Copilot CLI Agent Integration",
  "summary": "Implements `CopilotSDKBackend`, which wraps the `github-copilot-sdk` Python package to run GitHub Copilot as a PocketPaw agent. The SDK communicates with the `copilot` CLI via JSON-RPC internally, providing streaming, tool use, and multi-provider support (OpenAI, Azure, Anthropic, LiteLLM).",
  "concepts": [
    "CopilotSDKBackend",
    "github-copilot-sdk",
    "lazy initialisation",
    "event normalisation",
    "history injection",
    "multi-provider",
    "JSON-RPC",
    "tool policy mapping",
    "AgentEvent"
  ],
  "categories": [
    "agent-runtime",
    "github-copilot",
    "streaming"
  ],
  "source_docs": [
    "39a2557c699c9bca"
  ],
  "backlinks": null,
  "word_count": 439,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`CopilotSDKBackend` brings GitHub Copilot into PocketPaw's backend ecosystem. Rather than spawning the `copilot` CLI as a raw subprocess, it uses the `github-copilot-sdk` Python package, which wraps the CLI via an internal JSON-RPC channel and exposes a clean async API with event callbacks.

## Lazy Client Initialisation

`_ensure_client()` initialises `CopilotClient` on first use rather than at construction time. This deferred initialisation pattern serves two purposes. First, it avoids import-time failures: PocketPaw can start and list all available backends even when `github-copilot-sdk` is not installed — the missing dependency only surfaces when the user actually tries to use this backend. Second, it avoids paying SDK startup costs (CLI discovery, JSON-RPC handshake) until a message actually needs to be processed. The client is cached after first creation.

## Event Normalisation

The SDK delivers events through a registered callback. PocketPaw's `on_event` handler translates each SDK event into the appropriate `AgentEvent` variant (`TextChunk`, `ToolUse`, `ToolResult`, `Done`, `Error`). The helper `_get_event_type()` defensively handles two representations of event type: enum-valued (SDK older versions) and plain string (newer versions). SDK libraries frequently change internal representations between minor releases; this normaliser prevents the backend from breaking when users upgrade `github-copilot-sdk` without upgrading PocketPaw.

## History Injection via Instruction String

Copilot SDK does not expose a structured history API. `_inject_history()` prepends prior conversation turns as a formatted transcript in the instruction string passed to the SDK. This is the standard workaround for CLI-backed SDKs that are inherently stateless. The injected history is truncated to the most recent N turns to stay within context window limits.

## Multi-Provider Support

`BackendInfo.supported_providers` lists `copilot`, `openai`, `azure`, `anthropic`, and `litellm`. The Copilot SDK can route to different model providers under the hood, making this backend useful for teams that want Copilot-style UX with a non-default model provider.

## Tool Policy Mapping

Built-in tools (`shell`, `file_ops`, `git`, `web_search`) are mapped to PocketPaw policy categories:

| Tool | Policy Category |
|------|----------------|
| `shell` | `shell` |
| `file_ops` | `write_file` |
| `git` | `shell` |
| `web_search` | `browser` |

This ensures PocketPaw's trust-level security layer applies correctly regardless of which backend is active.

## Shutdown

`stop()` calls `client.close()` if the method exists. The defensive `hasattr` check handles SDK versions that do not expose an explicit close method.

## Known Gaps

- No `Capability.MCP` flag: MCP server integration is not yet implemented for this backend.
- The `on_event` callback model is synchronous; if the SDK fires events on a background thread, there is a risk of event reordering in async contexts.
- Session state is held in the SDK process; PocketPaw restart requires a new client and loses accumulated session context.
