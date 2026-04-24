---
{
  "title": "Provider Adapter Base Types: ProviderConfig, ProviderAdapter Protocol, and Model Resolution",
  "summary": "`base.py` defines the shared vocabulary for the entire provider adapter system: `ProviderConfig` (the resolved connection config), `ProviderAdapter` (the structural Protocol every adapter implements), and `resolve_model()` (the multi-tier model name fallback chain). These types are the contract between the LLM client layer and individual provider implementations.",
  "concepts": [
    "ProviderConfig",
    "ProviderAdapter",
    "Protocol",
    "resolve_model",
    "fallback chain",
    "frozen dataclass",
    "PROVIDER_DEFAULT_MODELS",
    "runtime_checkable",
    "backend model mapping"
  ],
  "categories": [
    "LLM integration",
    "provider adapter",
    "type system",
    "configuration"
  ],
  "source_docs": [
    "6fce0bb8339dbe6e"
  ],
  "backlinks": null,
  "word_count": 442,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Every provider adapter in PocketPaw — Anthropic, Ollama, Gemini, LiteLLM, OpenRouter — shares the same contract defined in `base.py`. This file answers three questions: what does a resolved provider config look like, what methods must every adapter expose, and how does the system pick the right model name given a backend and provider?

## ProviderConfig

```python
@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 0
    extra: dict[str, str] = field(default_factory=dict)
```

`ProviderConfig` is frozen to prevent accidental mutation after resolution — the same rationale as `LLMClient`. The `api_key=None` default covers providers like Ollama that run locally without authentication. `base_url=None` signals "use the provider's default endpoint." `max_tokens=0` means "use the provider's default limit." The `extra` dict is a forward-compatibility escape hatch for provider-specific fields that don't warrant a dedicated attribute.

## ProviderAdapter Protocol

```python
@runtime_checkable
class ProviderAdapter(Protocol):
    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig: ...
    def build_openai_client(self, config: ProviderConfig, **kwargs) -> Any: ...
    def build_anthropic_client(self, config: ProviderConfig, **kwargs) -> Any: ...
    def format_error(self, config: ProviderConfig, error: Exception, stderr: str) -> str: ...
```

Using `Protocol` rather than an abstract base class means adapters don't need to inherit from anything — they just need to implement the right methods. `runtime_checkable` allows `isinstance(adapter, ProviderAdapter)` checks at runtime, which is useful in tests and type narrowing.

Both `build_openai_client` and `build_anthropic_client` are required on all adapters even if a provider only supports one client type. Adapters that can't fulfill a method raise `NotImplementedError` with an actionable message, keeping the protocol uniform while signaling misuse clearly.

## Model Resolution Fallback Chain

```python
def resolve_model(settings: Settings, backend: str, provider: str) -> str:
```

Model resolution follows a priority chain:

1. Backend-specific setting (e.g., `settings.claude_sdk_model` for `claude_agent_sdk` backend)
2. Provider default model from `PROVIDER_DEFAULT_MODELS`

The `_BACKEND_MODEL_ATTR` dict maps backend names to settings attribute names, so adding a new backend only requires one dict entry rather than scattered `if/elif` chains.

`PROVIDER_DEFAULT_MODELS` provides last-resort fallbacks:

```python
PROVIDER_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "ollama": "llama3.2",
    "openai": "gpt-5.2",
    "gemini": "gemini-3-pro-preview",
    ...
}
```

Empty strings for `openai_compatible`, `openrouter`, and `litellm` signal that these providers have no sensible universal default — the user must configure a model explicitly.

## Known Gaps

- **No validation that `model` is non-empty**: if both the backend-specific setting and the provider default are empty strings, `resolve_model()` returns `""`. Callers will fail later with a cryptic API error rather than an explicit `ValueError` at resolution time.
- **`build_env_dict` is absent from the Protocol**: every concrete adapter implements `build_env_dict()`, but it is not declared in `ProviderAdapter`. This means type checkers won't catch adapters that omit it.