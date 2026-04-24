---
{
  "title": "OpenAICompatibleAdapter: Generic OpenAI-Compatible Endpoint Support",
  "summary": "`OpenAICompatibleAdapter` provides a catch-all provider adapter for any service that exposes an OpenAI-compatible REST API — vLLM, LocalAI, Together AI, Anyscale, and similar endpoints. It reads `openai_compatible_base_url` and `openai_compatible_api_key` from settings and constructs both `AsyncOpenAI` and `AsyncAnthropic` clients pointing at that endpoint.",
  "concepts": [
    "OpenAICompatibleAdapter",
    "vLLM",
    "LocalAI",
    "Together AI",
    "AsyncOpenAI",
    "AsyncAnthropic",
    "generic provider",
    "base_url",
    "openai_compatible"
  ],
  "categories": [
    "LLM integration",
    "OpenAI compatibility",
    "provider adapter",
    "self-hosted inference"
  ],
  "source_docs": [
    "22792c3917517b3f"
  ],
  "backlinks": null,
  "word_count": 416,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The AI ecosystem has converged on OpenAI's REST API as a de facto standard. Dozens of inference servers — vLLM, LocalAI, Together AI, Anyscale, Fireworks, Modal — expose an OpenAI-compatible endpoint. Rather than shipping a dedicated adapter for each, `OpenAICompatibleAdapter` provides a single generic adapter that works with any of them by reading `base_url` from settings.

## Config Resolution

```python
def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
    return ProviderConfig(
        provider=self.name,
        model=resolve_model(settings, backend, self.name),
        api_key=settings.openai_compatible_api_key,
        base_url=settings.openai_compatible_base_url,
    )
```

Unlike Ollama (which has a sensible default host) or Gemini (which has a fixed URL), `openai_compat` has no meaningful defaults. If `openai_compatible_base_url` is unset, both clients will be constructed with `base_url=None`, which typically means they fall through to the official OpenAI API — a reasonable fallback for users who just want standard OpenAI behavior.

## Dual Client Construction

Both `build_openai_client()` and `build_anthropic_client()` are fully implemented, unlike `AnthropicAdapter` which raises `NotImplementedError` on the OpenAI path. This matters because different agent backends use different client types:

- Claude Agent SDK uses `AsyncAnthropic`
- OpenAI Agents SDK uses `AsyncOpenAI`
- Google ADK may use either

The `openai_compat` adapter supports all of them by pointing both client types at the configured `base_url`.

## The `"not-needed"` Fallback

```python
api_key=config.api_key or "not-needed"
```

Some OpenAI-compatible servers (vLLM in permissive mode, LocalAI) accept any non-empty API key. Others require a specific key. The `"not-needed"` fallback keeps the client constructable even when `openai_compatible_api_key` is unset, deferring the actual authentication failure to the first API call where the error message will be more informative.

## Environment Variable Setup

```python
def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    if config.base_url:
        env["ANTHROPIC_BASE_URL"] = config.base_url
    env["ANTHROPIC_API_KEY"] = config.api_key or "not-needed"
    return env
```

`ANTHROPIC_BASE_URL` is conditionally set — if `base_url` is `None`, the env var is omitted entirely rather than set to an empty string. An empty `ANTHROPIC_BASE_URL` would cause the Anthropic SDK to attempt requests to an empty base URL, producing confusing errors.

## Error Formatting

`format_error()` includes the configured `base_url` in error messages, helping users confirm they've configured the right endpoint.

## Known Gaps

- **No URL validation**: if `openai_compatible_base_url` is set to an invalid URL (missing scheme, typo), the error surfaces at request time rather than at adapter initialization.
- **`build_anthropic_client` uses `base_url` directly**: vLLM and LocalAI may not support Anthropic's message format even at the same URL. Using `AsyncAnthropic` against a vLLM endpoint works only if the server implements Anthropic's API schema, which is not guaranteed for all OpenAI-compat servers.