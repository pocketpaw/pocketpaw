---
{
  "title": "LLMClient: Centralized Provider Detection, Client Creation, and Environment Setup",
  "summary": "`LLMClient` is an immutable descriptor that captures a fully resolved LLM provider configuration and exposes methods to create provider-specific async clients. The companion `resolve_backend_env()` function pushes the correct environment variables for the active backend, eliminating manual env-var wrangling when switching providers.",
  "concepts": [
    "LLMClient",
    "frozen dataclass",
    "provider detection",
    "client factory",
    "environment variables",
    "resolve_backend_env",
    "error formatting",
    "immutability",
    "provider abstraction"
  ],
  "categories": [
    "LLM integration",
    "provider management",
    "configuration",
    "error handling"
  ],
  "source_docs": [
    "7d322a710130a202"
  ],
  "backlinks": null,
  "word_count": 409,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Every agent backend in PocketPaw — Claude Agent SDK, OpenAI Agents, Google ADK — needs an LLM client. Before `LLMClient` existed, each backend had its own provider detection logic, leading to subtle inconsistencies: one backend might fall back to Ollama on missing API keys while another would raise immediately. `LLMClient` centralizes this logic.

## Immutability via `frozen=True`

`LLMClient` is a frozen dataclass:

```python
@dataclass(frozen=True)
class LLMClient:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    ...
```

Immutability prevents accidental mutation after resolution. A backend that receives an `LLMClient` cannot accidentally update `api_key` midway through a session — the value it got at startup is the value it uses throughout.

## Provider Detection Methods

The `is_*()` methods (`is_ollama()`, `is_anthropic()`, `is_openai_compatible()`, `is_gemini()`, `is_litellm()`, `is_openrouter()`) let callers branch on provider without string comparisons scattered across the codebase:

```python
if client.is_anthropic():
    sdk_client = client.create_anthropic_client(timeout=60)
else:
    sdk_client = client.create_openai_client(timeout=120)
```

This pattern is safer than `client.provider == "anthropic"` because it can include logic for provider families (e.g., `is_openai_compatible()` returning `True` for both `openai` and `openai_compatible` providers).

## Client Factories

`create_openai_client()` and `create_anthropic_client()` delegate to the appropriate provider adapter. They accept `timeout` and `max_retries` as keyword arguments so backends can set different values for streaming vs. non-streaming calls. The factories are lazy — no network call is made at creation time.

## Error Formatting

`format_api_error(error, stderr=...)` converts raw exception text into user-readable messages. The `stderr` parameter captures subprocess output from CLI-backed providers (like Ollama) that report errors on stderr rather than raising Python exceptions. Without unified error formatting, users would see raw tracebacks instead of actionable messages like "Anthropic API key not configured."

## Environment Variable Push

`resolve_backend_env(settings, force=False)` writes provider-specific environment variables (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, etc.) into `os.environ`. This matters for agent backends that spawn subprocesses or use libraries that read env vars directly rather than accepting config objects.

The `force` parameter controls whether existing env vars are overwritten. Without `force=True`, a user who manually sets `ANTHROPIC_BASE_URL` in their shell retains that value even if PocketPaw's settings say otherwise — respecting the principle of least surprise.

## Known Gaps

- **`_set()` method**: the private `_set(key, value)` method suggests there may be a path where `LLMClient` mutates itself after construction, which contradicts the `frozen=True` design. This should be audited.
- **No validation on construction**: `LLMClient` does not validate that `api_key` is present for providers that require it. Validation happens downstream when the client factory is called, producing a delayed error.