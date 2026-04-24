---
{
  "title": "LLM Provider Adapters Package: Public Interface for get_adapter",
  "summary": "The `pocketpaw.llm.providers` package re-exports `ProviderAdapter`, `ProviderConfig`, `get_adapter`, and `resolve_model` as its public API, giving callers a single import point for the provider adapter system. The init file documents the canonical usage pattern so it doubles as inline API documentation.",
  "concepts": [
    "provider adapter",
    "ProviderConfig",
    "get_adapter",
    "resolve_model",
    "package facade",
    "re-export pattern",
    "provider registry"
  ],
  "categories": [
    "LLM integration",
    "package structure",
    "provider management"
  ],
  "source_docs": [
    "6862b9fecdc49de1"
  ],
  "backlinks": null,
  "word_count": 358,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`pocketpaw/llm/providers/__init__.py` is the public facade for the LLM provider adapter subsystem. It re-exports four symbols from internal modules and documents the canonical three-step usage pattern that all callers should follow:

```python
from pocketpaw.llm.providers import get_adapter

adapter = get_adapter("anthropic")
config = adapter.resolve_config(settings, backend="claude_agent_sdk")
env = adapter.build_env_dict(config)
```

## The Four Public Symbols

- `ProviderAdapter` — the `Protocol` interface that every adapter must implement, enabling structural subtyping and `isinstance` checks
- `ProviderConfig` — the frozen dataclass holding a resolved provider connection config (model, API key, base URL, max tokens)
- `get_adapter` — the registry lookup function that returns a singleton adapter instance by provider name string
- `resolve_model` — the fallback-chain model resolution utility used by all adapters

## Why Document the Usage Pattern in the Init

Many packages expose symbols without explaining how they compose. By putting the three-step pattern directly in the module docstring, any developer who lands on `providers/__init__.py` — whether via IDE navigation, `help()`, or a search — immediately understands the intended workflow without reading the individual adapter files. This is especially important because the pattern is not obvious: `resolve_config` must precede `build_env_dict`, and the `backend` argument to `resolve_config` determines which model setting is read from `Settings`.

## Stable Import Surface

Internal reorganization is common as new providers are added. `GeminiAdapter` may be split into separate adapters for Vertex AI and Google AI Studio. `OpenAICompatibleAdapter` may be refactored. By surfacing only the four named symbols through this init, those changes remain invisible to callers who import from `pocketpaw.llm.providers`.

## Usage Context

The three-step pattern in the docstring covers the subprocess/env-var use case. For Python SDK client creation, the flow extends:

```python
from pocketpaw.llm.providers import get_adapter

adapter = get_adapter("ollama")
config = adapter.resolve_config(settings, backend="openai_agents")
client = adapter.build_openai_client(config, timeout=120.0)
```

## Known Gaps

- **`build_env_dict` is not on `ProviderAdapter`**: every adapter implements it, but it is not declared in the Protocol. The init re-exports `ProviderAdapter` without `build_env_dict`, so callers relying on type checking cannot call it without a cast.
- **No individual adapter classes exported**: if a caller needs to type-annotate against a specific adapter (e.g., `AnthropicAdapter`), they must import from `pocketpaw.llm.providers.anthropic` directly, breaking the facade pattern.