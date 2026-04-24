---
{
  "title": "Provider Adapter Registry: Centralized Adapter Lookup with Descriptive Errors",
  "summary": "The provider registry is a module-level dict mapping provider name strings to singleton adapter instances, with a `get_adapter()` function that raises `KeyError` with the list of available providers on unknown names. Pre-instantiating adapters as singletons avoids repeated construction overhead and provides a single point of truth for which providers PocketPaw supports.",
  "concepts": [
    "provider registry",
    "get_adapter",
    "singleton pattern",
    "adapter pattern",
    "KeyError",
    "ProviderAdapter",
    "dynamic dispatch",
    "module-level dict"
  ],
  "categories": [
    "LLM integration",
    "provider management",
    "registry pattern",
    "architecture"
  ],
  "source_docs": [
    "62248ac538439869"
  ],
  "backlinks": null,
  "word_count": 331,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`registry.py` is the authoritative catalog of all LLM providers PocketPaw supports. It maintains a module-level dict of singleton adapter instances and exposes `get_adapter()` as the canonical lookup mechanism.

## Registry Structure

```python
_ADAPTER_REGISTRY: dict[str, ProviderAdapter] = {
    "anthropic": AnthropicAdapter(),
    "ollama": OllamaAdapter(),
    "openai_compatible": OpenAICompatibleAdapter(),
    "openrouter": OpenRouterAdapter(),
    "gemini": GeminiAdapter(),
    "litellm": LiteLLMAdapter(),
}
```

Adapters are instantiated at module import time as singletons. This is safe because all adapter classes are stateless — they hold no mutable instance data. Pre-instantiation means `get_adapter()` is O(1) with no allocation on the hot path.

## Error Quality

```python
def get_adapter(provider: str) -> ProviderAdapter:
    try:
        return _ADAPTER_REGISTRY[provider]
    except KeyError:
        available = ", ".join(sorted(_ADAPTER_REGISTRY))
        raise KeyError(f"Unknown provider '{provider}'. Available: {available}") from None
```

The `from None` suppresses the original `KeyError`'s context, producing a cleaner traceback. The error message includes the sorted list of valid provider names so developers immediately know what values are accepted — no need to grep the source.

## Why a Registry vs. Direct Imports

Without a registry, callers would need to import the correct adapter class and instantiate it:

```python
from pocketpaw.llm.providers.anthropic import AnthropicAdapter
adapter = AnthropicAdapter()
```

This couples the caller to the internal module layout. With the registry, callers use a string name determined at runtime (from settings, CLI args, etc.), which is necessary for a system where the active provider is a user configuration choice.

## Adding a New Provider

To add a provider:
1. Create `pocketpaw/llm/providers/newprovider.py` implementing `ProviderAdapter`
2. Import and add to `_ADAPTER_REGISTRY` in `registry.py`
3. Add a default model in `PROVIDER_DEFAULT_MODELS` in `base.py`

The rest of the system — `LLMClient`, `resolve_backend_env`, error formatting — picks up the new provider automatically.

## Known Gaps

- **No dynamic registration**: adapters can only be added by modifying `registry.py`. A plugin system that reads adapter entrypoints would let third-party packages register custom providers.
- **No `openai` provider**: despite `PROVIDER_DEFAULT_MODELS` having an `"openai"` entry, there is no `OpenAIAdapter` in the registry. OpenAI access currently goes through `openai_compatible` with the official base URL.