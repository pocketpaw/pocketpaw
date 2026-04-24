---
{
  "title": "AgentBackend Protocol — Unified Interface for All Agent SDK Adapters",
  "summary": "Defines the structural `Protocol` and supporting types that every agent backend (Claude SDK, OpenAI Agents, Codex CLI, Google ADK, etc.) must satisfy. The contract enforces a consistent streaming `run()` generator and a static `info()` method so the router and loop can treat all backends polymorphically.",
  "concepts": [
    "AgentBackend",
    "Protocol",
    "Capability flags",
    "BackendInfo",
    "streaming generator",
    "AgentEvent",
    "tool_policy_map",
    "structural subtyping",
    "multi-backend"
  ],
  "categories": [
    "agent-runtime",
    "architecture",
    "protocols"
  ],
  "source_docs": [
    "9e4c25c7b073f934"
  ],
  "backlinks": null,
  "word_count": 582,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`backend.py` defines the contract layer for PocketPaw's pluggable agent architecture. Every concrete backend — `ClaudeSDKBackend`, `OpenAIAgentsBackend`, `CodexCLIBackend`, `CopilotSDKBackend`, `GoogleADKBackend`, `DeepAgentsBackend`, `OpenCodeBackend` — is validated against this structural `Protocol` at runtime. The file answers: "what must any agent SDK adapter expose?"

## Capability Flags

`Capability` is an `enum.Flag` (bitfield), allowing backends to advertise combinations of features without multiple boolean fields:

| Flag | Meaning |
|------|---------|
| `STREAMING` | Yields partial tokens as they arrive |
| `TOOLS` | Can invoke tool calls |
| `MCP` | Supports Model Context Protocol servers |
| `MULTI_TURN` | Maintains conversation history natively |
| `CUSTOM_SYSTEM_PROMPT` | Accepts an arbitrary system prompt |

Using a `Flag` rather than a list means capability checks are O(1) bitmask operations. Combining features is natural: `Capability.STREAMING | Capability.TOOLS | Capability.MCP`. The router can check `Capability.MCP in backend.info().capabilities` in a single expression.

## BackendInfo — Static Metadata Without an Instance

`BackendInfo` is a dataclass that describes a backend before it is instantiated:

- `name` / `display_name`: machine and human identifiers for config files and the UI
- `capabilities`: the bitmask of supported features
- `builtin_tools`: tool names the backend provides natively (e.g., `["Bash", "Read", "Write"]` for Claude SDK)
- `tool_policy_map`: maps each builtin tool name to a PocketPaw security policy category. This is critical — the security layer needs to know that Claude's `Bash` tool maps to the `shell` policy category, regardless of which backend is active.
- `required_keys`: environment variable names that must be present. PocketPaw can check these at startup and emit a helpful error (`"Set OPENAI_API_KEY to use openai_agents backend"`) before any request fails at runtime.
- `supported_providers`: LLM provider names the backend can talk to (e.g., `["openai", "ollama", "litellm"]`), used by the UI to populate provider dropdowns.
- `install_hint`: a dict with `pip_package`, `external_cmd`, `docs_url` — actionable install instructions emitted when a backend's dependency is missing.
- `beta`: marks experimental backends excluded from the default list.

`info()` is a `@staticmethod`, so callers can inspect backend metadata without creating an instance. This matters for backend discovery: the router reads `info()` from every registered backend class to build the capability registry, without paying the cost of initialising SDK clients or spawning subprocesses.

## AgentBackend Protocol

```python
class AgentBackend(Protocol):
    @staticmethod
    def info() -> BackendInfo: ...

    def __init__(self, settings: Settings) -> None: ...

    async def run(
        self,
        message: str,
        *,
        system_prompt: str,
        history: list,
        session_key: str,
    ) -> AsyncIterator[AgentEvent]: ...

    async def stop(self) -> None: ...
```

`run()` is an async generator. It yields `AgentEvent` objects — `TextChunk`, `ToolUse`, `ToolResult`, `Done`, `Error` — as they arrive from the underlying SDK. This push model means `AgentLoop` can forward incremental tokens to channels in real time rather than buffering a complete response.

`stop()` is a cooperative shutdown signal. Subprocess-based backends (Codex CLI, Gemini CLI) use it to send `SIGTERM` to child processes. SDK-based backends use it to cancel pending HTTP requests. Without `stop()`, cancelling an in-flight session would leave orphaned processes and open connections.

## Why a Protocol Over an Abstract Base Class

Python `Protocol` uses structural subtyping: a class satisfies the protocol if it has the right methods with the right signatures, regardless of inheritance. This means third-party backends can implement `AgentBackend` without importing from PocketPaw's codebase — useful for plugin scenarios. An ABC would require explicit inheritance and create a tight coupling.

## Known Gaps

None. This file is intentionally stable — changes here are breaking changes for all backends and require coordinated updates across the entire backend registry.
