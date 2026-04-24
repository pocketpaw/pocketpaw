---
{
  "title": "AnthropicAdapter: Native Anthropic API Configuration and Client Construction",
  "summary": "`AnthropicAdapter` implements the `ProviderAdapter` protocol for Anthropic's native API, resolving model and API key from settings and constructing `AsyncAnthropic` clients. It explicitly raises `NotImplementedError` on `build_openai_client()` to enforce that Anthropic-native callers never accidentally get an OpenAI-compatible client.",
  "concepts": [
    "AnthropicAdapter",
    "AsyncAnthropic",
    "ProviderAdapter",
    "API key",
    "NotImplementedError guard",
    "error formatting",
    "model resolution",
    "environment variables"
  ],
  "categories": [
    "LLM integration",
    "Anthropic",
    "provider adapter",
    "client construction"
  ],
  "source_docs": [
    "0aa4f91f65a74373"
  ],
  "backlinks": null,
  "word_count": 357,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`AnthropicAdapter` is the provider adapter for Anthropic's native API. It handles three responsibilities: resolving the model name and API key from `Settings`, building `AsyncAnthropic` client instances, and formatting authentication errors into user-readable messages.

## Config Resolution

```python
def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
    return ProviderConfig(
        provider=self.name,
        model=resolve_model(settings, backend, self.name),
        api_key=settings.anthropic_api_key,
    )
```

Note that `base_url` is omitted — `ProviderConfig` defaults it to `None`, which signals to `AsyncAnthropic` to use the official `api.anthropic.com` endpoint. This is intentional: unlike Ollama or LiteLLM, the Anthropic adapter is never routed through a proxy.

## Environment Variables

`build_env_dict()` returns only `ANTHROPIC_API_KEY`. It omits `ANTHROPIC_BASE_URL` entirely, rather than setting it to a default value. This matters because the Claude Agent SDK reads `ANTHROPIC_BASE_URL` if present and uses it to override the default endpoint — setting it to an empty string or a default would suppress any legitimate environment-level overrides.

## Client Construction

`build_anthropic_client()` returns `AsyncAnthropic` directly with a 60-second timeout and 2 retries. The shorter timeout (vs. 120s in most other adapters) reflects that Anthropic's cloud API is expected to be responsive; a 60s wait before surfacing an error is already generous.

## Explicit OpenAI Client Rejection

```python
def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
    raise NotImplementedError(
        "AnthropicAdapter does not support OpenAI clients. "
        "Use build_anthropic_client() instead."
    )
```

This is a guard against misuse. The `ProviderAdapter` protocol requires both `build_openai_client` and `build_anthropic_client`. Without the explicit `NotImplementedError`, a generic call to `adapter.build_openai_client()` would silently fail with an `AttributeError` or return `None`. The error message names the correct alternative, making it actionable.

## Error Formatting

The `format_error()` method checks for authentication keywords in the lowercased error string. If found, it returns a UI-friendly message pointing users to Settings. This avoids surfacing raw Anthropic API error JSON to non-technical users.

## Known Gaps

- **No prompt caching configuration**: Anthropic's API supports prompt caching via beta headers. The current `AsyncAnthropic` instantiation does not set `default_headers` with `anthropic-beta: prompt-caching-2024-07-31`. This may result in higher costs for repeated system prompts.
- **Hardcoded timeout**: the 60s timeout is not configurable via `Settings`. High-latency use cases (long documents, extended thinking) may need a higher value.