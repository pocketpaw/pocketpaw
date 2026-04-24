---
{
  "title": "LLM Package Public API: LLMClient, LLMRouter, and resolve_llm_client",
  "summary": "The `pocketpaw.llm` package exposes three public symbols — `LLMClient`, `LLMRouter`, and `resolve_llm_client` — forming the canonical entry point for all LLM interactions in PocketPaw. This init file enforces a stable import surface so internal module restructuring does not break callsites.",
  "concepts": [
    "LLMClient",
    "LLMRouter",
    "resolve_llm_client",
    "package init",
    "public API",
    "re-export pattern",
    "provider abstraction"
  ],
  "categories": [
    "LLM integration",
    "package structure",
    "API surface"
  ],
  "source_docs": [
    "bb6ac2327fb904e5"
  ],
  "backlinks": null,
  "word_count": 371,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`pocketpaw/llm/__init__.py` is a thin re-export module that forms the stable public API surface for all LLM interactions in PocketPaw. It collects three symbols from internal submodules and makes them available at the package level:

- `LLMClient` — an immutable descriptor representing a fully resolved LLM provider configuration
- `LLMRouter` — a lightweight utility for simple, tool-free chat completions used by non-agent callers like Guardian AI and diagnostic scripts
- `resolve_llm_client` — the factory function that reads active settings and returns the appropriate `LLMClient`

## Why a Dedicated Init

Without this init, callers would import directly from submodules:

```python
from pocketpaw.llm.client import LLMClient, resolve_llm_client
from pocketpaw.llm.router import LLMRouter
```

If `client.py` is later split into `client_core.py` and `client_factory.py`, every callsite in the codebase breaks. The init file absorbs that churn — the public contract stays at `from pocketpaw.llm import LLMClient` regardless of internal layout changes. This pattern is especially valuable in a system where the LLM subsystem is actively evolving: new provider adapters, streaming support, and caching layers can be added without touching call sites.

## Relationship Between the Three Exports

`resolve_llm_client` is the entry point: it reads `Settings` and returns an `LLMClient` configured for the active provider. Agent backends receive the resulting `LLMClient` and use its `create_openai_client()` or `create_anthropic_client()` methods to get SDK-level client objects.

`LLMRouter` is a parallel, simpler path: it bypasses the full provider adapter system and does its own backend detection for one-off completions. The two paths are intentionally separate — `LLMRouter` exists for callers that do not need the full agent pipeline.

## Usage

```python
from pocketpaw.llm import LLMClient, LLMRouter, resolve_llm_client

# Agent backend path
client: LLMClient = resolve_llm_client(settings)
sdk_client = client.create_anthropic_client(timeout=60)

# Lightweight diagnostic path
router = LLMRouter(settings)
response = await router.chat("Is the system healthy?")
```

## Known Gaps

- **No version or deprecation markers**: as the LLM subsystem evolves, there is no mechanism to mark `LLMRouter` as deprecated in favor of a future higher-level abstraction. Adding `__deprecated__` annotations or runtime warnings would help signal intent to consumers.
- **`__all__` is implicit**: the three symbols are imported at module level but there is no explicit `__all__` list in this init. Adding `__all__` would prevent accidental re-export of internal imports if the init is extended.