---
{
  "title": "LiteLLMAdapter: Proxy and Direct SDK Modes for Universal LLM Routing",
  "summary": "`LiteLLMAdapter` supports two integration modes — a proxy mode that routes through a running LiteLLM server via OpenAI-compatible HTTP, and a direct SDK mode that uses LiteLLM's native Python wrappers when available. This dual-mode design lets PocketPaw work with any model LiteLLM supports without requiring each deployment to run a proxy server.",
  "concepts": [
    "LiteLLMAdapter",
    "proxy mode",
    "direct SDK mode",
    "LitellmModel",
    "OpenAI-compatible",
    "AsyncOpenAI",
    "universal LLM routing",
    "environment variables",
    "lazy import"
  ],
  "categories": [
    "LLM integration",
    "LiteLLM",
    "provider adapter",
    "proxy pattern"
  ],
  "source_docs": [
    "e3cef55578b03f41"
  ],
  "backlinks": null,
  "word_count": 380,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

LiteLLM is a universal LLM proxy that normalizes 100+ model APIs behind a single OpenAI-compatible interface. `LiteLLMAdapter` integrates it into PocketPaw's provider system in two modes:

1. **Proxy mode**: a LiteLLM server runs separately (e.g., `litellm --model gpt-4`), and PocketPaw talks to it over HTTP like any OpenAI-compatible endpoint.
2. **Direct SDK mode**: LiteLLM is imported as a Python library and used natively via `LitellmModel` or `LiteLlm` wrappers.

## Config Resolution

```python
def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
    return ProviderConfig(
        provider=self.name,
        model=resolve_model(settings, backend, self.name),
        api_key=settings.litellm_api_key,
        base_url=settings.litellm_api_base.rstrip("/"),
        max_tokens=settings.litellm_max_tokens,
    )
```

The `base_url` determines which mode is active: if `litellm_api_base` is set (typically `http://localhost:4000`), proxy mode is used. If it is empty or absent, direct SDK mode applies.

## Proxy Mode Client Construction

```python
def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
    from openai import AsyncOpenAI
    base = (config.base_url or "http://localhost:4000").rstrip("/")
    return AsyncOpenAI(base_url=f"{base}/v1", api_key=config.api_key or "not-needed", ...)
```

The `/v1` suffix append is defensive: LiteLLM's OpenAI-compatible endpoint lives at `/v1/chat/completions`. If a user configures `base_url` as `http://localhost:4000/v1` already, the double-suffix would break. The code strips trailing slashes before appending to handle the common case, but does not check for pre-existing `/v1`.

## Direct SDK Mode: build_agents_model and build_adk_model

`build_agents_model()` constructs a `LitellmModel` object for the OpenAI Agents SDK. `build_adk_model()` constructs a `LiteLlm` object for the Google ADK. Both import lazily so that the `litellm` package is not required when running in proxy mode.

## Environment Variable Setup

```python
def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": config.base_url or "http://localhost:4000",
        "ANTHROPIC_API_KEY": config.api_key or "not-needed",
    }
```

Using `ANTHROPIC_*` env vars for a LiteLLM proxy works because the Claude Agent SDK reads these to determine its base URL. When LiteLLM is the intermediary, it translates the Anthropic-formatted request to whatever backend model is configured.

## Error Formatting

`format_error()` checks for common LiteLLM proxy error patterns: connection refused (proxy not running), auth failures, and model-not-found errors. Each gets a specific guidance message.

## Known Gaps

- **`/v1` double-suffix risk**: if `litellm_api_base` is configured as `http://localhost:4000/v1`, the client URL becomes `http://localhost:4000/v1/v1`. Validation or normalization of the base URL would prevent this.
- **Direct SDK mode is best-effort**: `build_agents_model()` and `build_adk_model()` import lazily; if `litellm` is not installed, they raise `ImportError` at call time rather than failing fast at adapter initialization.