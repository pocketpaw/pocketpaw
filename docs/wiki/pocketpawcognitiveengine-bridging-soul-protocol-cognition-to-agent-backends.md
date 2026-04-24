---
{
  "title": "PocketPawCognitiveEngine: Bridging Soul Protocol Cognition to Agent Backends",
  "summary": "`PocketPawCognitiveEngine` implements the Soul Protocol's `CognitiveEngine` interface, routing fact-extraction and reflection calls to PocketPaw's active agent backend or to a cheaper dedicated Anthropic model when configured. It enables the soul's cognitive layer to reuse the existing LLM infrastructure rather than requiring a separate AI provider setup.",
  "concepts": [
    "CognitiveEngine",
    "PocketPawCognitiveEngine",
    "backend_provider",
    "lazy resolution",
    "Anthropic SDK",
    "soul cognition",
    "graceful degradation",
    "streaming",
    "AgentEvent",
    "cost optimization",
    "cognitive tasks"
  ],
  "categories": [
    "soul-protocol",
    "agent-backends",
    "llm-integration"
  ],
  "source_docs": [
    "20e2614ce372e176"
  ],
  "backlinks": null,
  "word_count": 471,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Every Soul Protocol soul periodically needs to perform cognitive tasks: extracting facts from conversation, scoring the significance of memories, and reflecting on recent interactions. The `CognitiveEngine` protocol in `soul-protocol` defines the interface for these tasks. `PocketPawCognitiveEngine` is PocketPaw's concrete implementation of that interface.

## Lazy Backend Resolution

The engine accepts a `backend_provider: Callable[[], AgentBackend | None]` rather than an `AgentBackend` directly. This lazy callable pattern is critical because:

- The agent backend is created per-session and may not exist at the time `SoulManager` is initialized.
- Passing a factory avoids holding a stale reference if the backend changes between sessions.
- If the provider returns `None` (backend unavailable), the engine falls back gracefully.

## Dual-Path Execution

When the optional `model` parameter is set (e.g., `"claude-haiku-4-5-20251001"`), the engine bypasses the main backend entirely and calls the Anthropic Messages API directly via the `anthropic` SDK. This was added in April 2026 to address a cost concern: soul cognitive tasks fire 5–6 times per user message, and running them through the full agent backend (with its system prompt, tool registration overhead, and larger model) was significantly more expensive than using a lightweight Haiku call.

The two paths are:
1. **Direct Anthropic API path** (when `model` is set) — constructs a minimal messages request, streams the response, and concatenates content blocks.
2. **Backend streaming path** (default) — calls `_stream_to_text(backend, prompt)`, which iterates `AgentEvent` objects from the backend's streaming interface and concatenates only `message`-typed events.

## Graceful Degradation

Both execution paths are wrapped in broad exception handlers that return an empty string on failure. This is intentional: soul cognitive tasks are enhancement features. If the engine fails (backend unavailable, API key missing, network error), the soul falls back to its built-in heuristic scoring rather than crashing the user's session. An empty string signals to the soul that no enriched cognition occurred.

```python
async def think(self, prompt: str) -> str:
    if self._model:
        # Try direct Anthropic API first
        try:
            return await self._direct_anthropic_call(prompt)
        except Exception:
            pass  # Fall through to backend
    backend = self._backend_provider()
    if backend is None:
        return ""
    try:
        return await self._stream_to_text(backend, prompt)
    except Exception:
        return ""
```

## Session Key Isolation

The module-level `_cognitive_session_key()` function generates a unique key for each cognitive session. This prevents soul cognitive calls from polluting the conversation history of the user's active session — cognitive introspection should be invisible to the user-facing agent loop.

## Known Gaps

- **No retry logic** — transient API failures (rate limits, network blips) result in empty strings rather than retries. For high-importance cognitive tasks (long-term memory consolidation), silent failure means data is never extracted.
- **Streaming overhead for short prompts** — the backend streaming path was designed for conversational responses. For the short, structured prompts used in cognition (fact extraction returns JSON, not prose), a non-streaming completion call would be more efficient.