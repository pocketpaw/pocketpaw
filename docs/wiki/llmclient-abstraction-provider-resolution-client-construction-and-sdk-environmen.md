---
{
  "title": "LLMClient Abstraction: Provider Resolution, Client Construction, and SDK Environment Setup",
  "summary": "The `LLMClient` tests validate the centralized abstraction over multiple LLM backends — Anthropic, OpenAI, Ollama, and OpenRouter — ensuring that provider auto-detection, client instantiation, and SDK environment variable injection all behave correctly for each provider and credential combination. The frozen dataclass design prevents accidental mutation after resolution.",
  "concepts": [
    "LLMClient",
    "provider auto-resolution",
    "Anthropic",
    "OpenAI",
    "Ollama",
    "OpenRouter",
    "resolve_llm_client",
    "SDK environment variables",
    "frozen dataclass",
    "error formatting",
    "openai_compatible"
  ],
  "categories": [
    "LLM integration",
    "configuration",
    "test"
  ],
  "source_docs": [
    "396633d67512af2b"
  ],
  "backlinks": null,
  "word_count": 478,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple LLM backends and must route requests to the right one based on which API keys are configured. Rather than scattering provider-selection logic across the codebase, the `LLMClient` dataclass and `resolve_llm_client()` factory centralize this logic. The test suite proves that resolution, construction, environment setup, and error formatting all work correctly for every supported combination.

## Provider Auto-Resolution

`resolve_llm_client(settings)` implements a priority cascade when `llm_provider="auto"`:

1. **Anthropic** — preferred if `anthropic_api_key` is set.
2. **OpenAI** — fallback if only `openai_api_key` is set.
3. **Ollama** — last resort when no cloud keys are present.

The tests `test_resolve_auto_prefers_anthropic_over_openai` makes the priority explicit: when both keys exist, Anthropic wins. This prevents users with both keys configured from accidentally paying OpenAI rates when they intended to use Claude.

`test_resolve_force_provider` validates the `force_provider` override, which allows callers to bypass auto-detection. This is used internally when the router needs to try a specific backend.

`test_resolve_openrouter` confirms that `force_provider="openrouter"` resolves to `provider="openai_compatible"` with the OpenRouter base URL injected — the production code maps the human-facing name to the underlying transport.

## Client Construction

`TestCreateAnthropicClient` patches `anthropic.AsyncAnthropic` and verifies the constructor arguments:

- **Ollama**: uses a custom `base_url` (the Ollama host), a dummy `api_key`, and explicit `timeout`/`max_retries` values.
- **Anthropic**: uses the real API key and default transport settings.
- **Custom timeout**: confirms that `Settings.llm_timeout` is passed through.
- **OpenAI**: raises `NotImplementedError` because `create_client()` is only valid for Anthropic-compatible transports; OpenAI uses a separate client factory.

## SDK Environment Setup

`TestToSdkEnv` tests `LLMClient.to_sdk_env()`, which returns a dict of environment variables for subprocess-based SDK invocations. The critical cases:

- **OpenRouter**: sets `ANTHROPIC_AUTH_TOKEN` to the OpenRouter key and blanks `ANTHROPIC_API_KEY`. This works around the Anthropic SDK's env-variable precedence — if `ANTHROPIC_API_KEY` is non-empty, the SDK ignores `ANTHROPIC_AUTH_TOKEN`. The blank-out is an intentional workaround for SDK behavior.
- **OpenRouter with `/v1` suffix**: the base URL must have `/v1` stripped before injection, because the SDK appends it internally.
- **Non-OpenRouter compatible provider**: returns an empty dict — no environment manipulation needed.
- **No key**: returns an empty dict without crashing.

## Error Formatting

`TestFormatApiError` tests `LLMClient.format_api_error()`, which translates low-level SDK exceptions into human-readable messages:

- Ollama "model not found" → actionable message about pulling the model.
- Ollama connection refused → actionable message about starting the Ollama service.
- Anthropic 401 → message about invalid API key.

These formatted messages surface in the chat UI rather than raw exception tracebacks.

## Immutability Guard

`TestFrozen` confirms that `LLMClient` is a frozen dataclass — attempts to mutate fields after construction raise `dataclasses.FrozenInstanceError`. This prevents configuration drift after the client is resolved and cached.

## Known Gaps

- There is no test for concurrent resolution — if multiple coroutines call `resolve_llm_client()` simultaneously with `force_provider`, the behavior is untested.
- The OpenRouter `/v1` strip logic (`test_to_sdk_env_openrouter_strips_v1`) is tested with only one URL pattern; trailing slashes or other variants are not covered.