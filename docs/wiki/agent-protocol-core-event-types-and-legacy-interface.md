---
{
  "title": "Agent Protocol: Core Event Types and Legacy Interface",
  "summary": "Defines the standardized `AgentEvent` dataclass that all agent backends emit, and the `AgentProtocol` structural typing interface that existed before the more capable `AgentBackend` ABC replaced it. Together they form the lingua franca for streaming agent output across PocketPaw's multi-backend architecture.",
  "concepts": [
    "AgentEvent",
    "AgentProtocol",
    "streaming events",
    "async iterator",
    "structural typing",
    "typing.Protocol",
    "agent backends",
    "tool_use event",
    "thinking event",
    "event normalization"
  ],
  "categories": [
    "agents",
    "protocols",
    "streaming",
    "type system"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 507,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`protocol.py` establishes the two foundational primitives of PocketPaw's agent layer: a data shape for agent output (`AgentEvent`) and a structural typing contract for agent implementations (`AgentProtocol`).

## AgentEvent — The Unified Output Token

Every message, thought, tool call, or error that an agent backend produces is packaged into an `AgentEvent` dataclass before reaching the rest of the system. This uniformity matters because PocketPaw supports multiple backends (Claude Agent SDK, OpenAI Agents, Google ADK, etc.), each with their own native event formats. By converting everything to `AgentEvent` at the boundary, all downstream consumers — the WebSocket layer, the message bus, the dashboard UI — work identically regardless of which backend is active.

The recognized `type` values are:

- **`message`** — streamed text content from the agent
- **`tool_use`** — the agent is invoking a tool (name, inputs visible)
- **`tool_result`** — the tool returned a result
- **`thinking`** — extended thinking content, surfaced in the Activity panel only
- **`thinking_done`** — signals the end of a thinking phase
- **`token_usage`** — token consumption metadata for billing/monitoring
- **`error`** — an error from the backend or a tool
- **`done`** — the agent has finished its turn

The `content` field is typed `Any` because different event types carry structurally different payloads (strings, dicts, `None`). The `metadata` dict provides an open-ended extension point for backend-specific fields (e.g., model version, stop reason) without polluting the core type.

Using `field(default_factory=dict)` for `metadata` prevents the classic Python mutable-default-argument bug where all instances would share the same dictionary.

## AgentProtocol — The Legacy Interface

`AgentProtocol` is a `typing.Protocol` (structural subtyping) that declares three async methods: `run`, `stop`, and `get_status`. It was the original contract for agent backends before the codebase grew complex enough to warrant a proper abstract base class (`AgentBackend`).

The docstring says explicitly: **"Legacy interface kept for type-checking compatibility."** This means real backends no longer inherit from `AgentProtocol` — they inherit from `AgentBackend` (defined in `backend.py`). `AgentProtocol` survives because existing mypy annotations and test fixtures still reference it, and removing it would cause type errors without meaningful runtime benefit.

The `run` method signature reflects the minimal surface that all backends must support: a user message, an optional system prompt, and optional conversation history. The async iterator return type (`AsyncIterator[AgentEvent]`) encodes the streaming-first design — consumers process events as they arrive rather than waiting for a complete response.

## Design Rationale

The decision to define a separate protocol module rather than inline these types in `backend.py` or `router.py` prevents circular imports. `protocol.py` imports nothing from PocketPaw itself — only stdlib. This means any module in the agents package can safely import `AgentEvent` without pulling in heavier dependencies like settings or registry.

## Known Gaps

- `AgentProtocol` is acknowledged as legacy and may be removed in a future major version once all annotation sites migrate to `AgentBackend`. There is no migration timeline documented.
- The `content: Any` field on `AgentEvent` makes static analysis of event consumers difficult. A future refactor could use a `Union` of typed variants or a discriminated union pattern.
