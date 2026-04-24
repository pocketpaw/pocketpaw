---
{
  "title": "OpenRouterAdapter: Multi-Model Routing with Anthropic-Compat Authentication Quirks",
  "summary": "`OpenRouterAdapter` integrates OpenRouter's model routing service, which provides access to 100+ models via a single API key. Its most distinctive behavior is an intentional `ANTHROPIC_API_KEY=\"\"` (empty string) workaround: OpenRouter authenticates via `ANTHROPIC_AUTH_TOKEN`, and if `ANTHROPIC_API_KEY` is set to a real value, the Anthropic SDK sends it as `Bearer` and OpenRouter rejects the request.",
  "concepts": [
    "OpenRouterAdapter",
    "OPENROUTER_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "authentication workaround",
    "empty API key",
    "model routing",
    "AsyncOpenAI",
    "AsyncAnthropic",
    "provider aggregator"
  ],
  "categories": [
    "LLM integration",
    "OpenRouter",
    "provider adapter",
    "authentication"
  ],
  "source_docs": [
    "b1cd12f881ffa7d2"
  ],
  "backlinks": null,
  "word_count": 360,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

OpenRouter is an API aggregator that routes requests to Anthropic, OpenAI, Google, Mistral, and dozens of other providers under a single API key and unified billing. `OpenRouterAdapter` handles the peculiarities of OpenRouter's authentication scheme, which diverges from standard Anthropic API conventions.

## The Authentication Workaround

This is the most important part of the adapter:

```python
def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
    base_url = (config.base_url or OPENROUTER_BASE_URL).rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3].rstrip("/")
    env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "",   # Must be empty string, not omitted
    }
    if config.api_key:
        env["ANTHROPIC_AUTH_TOKEN"] = config.api_key
    return env
```

The comment explains the failure: OpenRouter's Anthropic-compatible skin authenticates via `ANTHROPIC_AUTH_TOKEN`, not `ANTHROPIC_API_KEY`. If `ANTHROPIC_API_KEY` is set to a real value, the Anthropic Python SDK sends it as an `Authorization: Bearer <key>` header, and OpenRouter rejects it with a 401. Setting `ANTHROPIC_API_KEY` to an empty string suppresses the Bearer header while still satisfying the SDK's non-None validation.

## Base URL Stripping

OpenRouter's Anthropic-compatible endpoint lives at `https://openrouter.ai/api` (without `/v1`), while its OpenAI-compatible endpoint lives at `https://openrouter.ai/api/v1`. The env dict strips `/v1` from the base URL before setting `ANTHROPIC_BASE_URL`, because the Anthropic SDK appends its own path components. Forgetting this stripping causes double-path errors.

## Model Fallback

```python
model = resolve_model(settings, backend, self.name)
if not model:
    model = settings.openai_compatible_model
```

OpenRouter has no meaningful universal default model — the right choice depends on the use case and cost constraints. If no OpenRouter-specific model is configured, it falls back to `openai_compatible_model`, giving users a way to share one model config across compatible providers.

## Client Construction

`build_openai_client()` points `AsyncOpenAI` at `OPENROUTER_BASE_URL` with the OpenRouter API key. `build_anthropic_client()` also points at OpenRouter's base (without `/v1`) using the same key. Both set `max_retries=1` — OpenRouter's routing layer can return transient 429s, but aggressive retries would worsen load balancing.

## Known Gaps

- **Empty `ANTHROPIC_API_KEY` could break downstream validation**: any code that checks `os.environ.get("ANTHROPIC_API_KEY")` and treats empty string as "not configured" would incorrectly conclude Anthropic is unconfigured. The workaround trades one failure mode for another.
- **No OpenRouter-specific headers**: OpenRouter supports `HTTP-Referer` and `X-Title` headers for analytics and rate limit exemptions. These are not set by the adapter.