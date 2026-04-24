---
{
  "title": "PocketPaw Compiler Backend Adapter for Knowledge Base",
  "summary": "Implements `PocketPawCompilerBackend`, a `CompilerBackend` Protocol adapter that makes PocketPaw's active agent LLM backend available to the `knowledge_base` package's compiler pipeline. This enables KB article compilation to use whatever AI backend is configured in the agent registry -- Claude, OpenAI, or any other -- without hardcoding a specific model.",
  "concepts": [
    "PocketPawCompilerBackend",
    "CompilerBackend Protocol",
    "agent registry",
    "knowledge_base compiler",
    "complete method",
    "streaming concatenation",
    "LLM backend",
    "adapter pattern",
    "backend_name",
    "model selection",
    "ee.cloud.kb",
    "standalone package bridge"
  ],
  "categories": [
    "knowledge-base",
    "compiler",
    "ai-backend",
    "cloud"
  ],
  "source_docs": [
    "25a02b248d4a60ea"
  ],
  "backlinks": null,
  "word_count": 488,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`PocketPawCompilerBackend` is the bridge between two independently developed systems: the standalone `knowledge_base` Python package (which compiles source code into searchable wiki articles) and PocketPaw's agent backend registry (which manages connections to LLM APIs like Anthropic Claude and OpenAI). Without this adapter, the compiler would need its own API key configuration and model selection logic, duplicating what the agent registry already manages.

## The CompilerBackend Protocol

The `knowledge_base` package defines a minimal Protocol for its compilation backend:

```python
async def complete(prompt: str, system_prompt: str = "") -> str
```

Any object with this method satisfies the Protocol. `PocketPawCompilerBackend` implements exactly this interface, making it a drop-in backend for the compiler without any changes to the `knowledge_base` package itself.

## Agent Registry Delegation

At construction time, `PocketPawCompilerBackend` accepts an optional `backend_name` and `model` string:

```python
def __init__(self, backend_name: str = "", model: str = "") -> None:
    self._backend_name = backend_name
    self._model = model
```

When `complete` is called, the adapter looks up the named backend from PocketPaw's agent registry (or falls back to the active default if `backend_name` is empty), constructs the appropriate SDK call, and streams the response, concatenating message chunks into a single string return value.

This design means that changing PocketPaw's default LLM backend (from Claude Sonnet to Claude Opus, for example) automatically affects KB compilation without any changes to the KB pipeline configuration.

## Streaming and Concatenation

The adapter streams the LLM response and concatenates chunks. This is necessary because PocketPaw's agent backends are designed for streaming use cases (chat interfaces, real-time output). The compiler's `complete` interface expects a complete string, so the adapter must bridge the streaming-to-synchronous boundary. Buffering the entire response is acceptable here because KB article compilation prompts are not interactive and the results are stored, not displayed in real time.

## Created Date as Documentation

The file header includes `# Created: 2026-04-06`, which aligns with the soul memory record of the "marathon session" that built and shipped the `knowledge_base` package. This timestamp helps future contributors trace when the bridge was introduced relative to the broader kb-go feature rollout.

## Why an Adapter Rather Than Direct Import?

The `knowledge_base` package is designed as a standalone, reusable library that does not depend on PocketPaw. Direct imports from `ee.*` inside `knowledge_base` would break that independence. The adapter pattern keeps the dependency arrow pointing inward: `ee.cloud.kb` depends on `knowledge_base`, not the reverse.

## Known Gaps

- **No timeout or token limit.** Long compilation prompts could generate very long responses. Without a timeout, a slow or runaway LLM response would block the compiler indefinitely.
- **Error handling is unspecified.** If the agent backend raises an exception (rate limit, network error), the adapter's behaviour depends on whether the underlying backend propagates or swallows exceptions -- this is not documented.
- **`backend_name` fallback logic is implicit.** The empty-string default means "use the active default backend," but this convention is not type-enforced or documented in the method signature.