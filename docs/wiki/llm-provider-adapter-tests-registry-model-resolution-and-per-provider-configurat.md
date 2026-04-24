---
{
  "title": "LLM Provider Adapter Tests: Registry, Model Resolution, and Per-Provider Configuration",
  "summary": "PocketPaw supports multiple LLM providers (Anthropic, Ollama, OpenRouter, Gemini, LiteLLM) through a unified adapter pattern. These tests validate the provider registry, model string resolution priority, per-adapter environment variable construction, and the `LLMClient` delegation layer that dispatches to the correct adapter.",
  "concepts": [
    "provider adapters",
    "LLM registry",
    "model resolution",
    "Anthropic adapter",
    "Ollama adapter",
    "OpenRouter adapter",
    "Gemini adapter",
    "LiteLLM adapter",
    "LLMClient",
    "env dict",
    "error formatting"
  ],
  "categories": [
    "testing",
    "LLM providers",
    "configuration",
    "test"
  ],
  "source_docs": [
    "9febe2f9b6eac409"
  ],
  "backlinks": null,
  "word_count": 509,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw must work across different LLM backends without requiring users to reconfigure each one from scratch. The adapter pattern encapsulates provider-specific SDK initialization, environment variable names, URL conventions, and error message formatting. These tests ensure each adapter is correctly configured and that the registry dispatches to the right one.

## Provider Registry

`TestRegistry` verifies two behaviors:

- **Known providers**: `anthropic`, `ollama`, `openrouter`, `gemini`, and `litellm` are all registered.
- **Unknown provider raises**: requesting an unregistered provider raises a clear error rather than returning `None` silently.

A silent `None` return would cause a confusing `AttributeError` deep in the call stack rather than an actionable "unknown provider" message.

## Model Resolution Priority

`TestResolveModel` validates the three-tier model resolution hierarchy:

1. **Backend-specific setting wins**: a model configured for a specific backend (e.g., `anthropic_model`) takes highest priority.
2. **Provider-level fallback**: the provider's default model is used if no backend-specific setting exists.
3. **Global default**: a system-wide default model is used as last resort.
4. **LiteLLM passthrough**: LiteLLM model strings are passed through unchanged.

This hierarchy allows operators to set a global default while overriding it for specific backends — useful in multi-backend deployments.

## Per-Adapter Tests

### Anthropic
`TestAnthropicAdapter` verifies:
- `resolve_config()` returns a config with the correct model and endpoint.
- `build_env_dict()` maps the Anthropic API key to the correct env var name.
- Missing API key produces an empty dict (no crash, just no key).
- Auth errors produce a formatted message directing users to check their API key.

### Ollama
`TestOllamaAdapter` is the most locally-focused adapter:
- No API key required — `resolve_config()` works without credentials.
- `build_openai_client_appends_v1()` ensures the OpenAI-compat URL is `http://localhost:11434/v1`, not `http://localhost:11434` — the `/v1` suffix is required by the OpenAI SDK and is a common misconfiguration.
- Error messages distinguish between "model not found" and "connection refused" — actionable for local setup debugging.

### OpenRouter
`TestOpenRouterAdapter` verifies:
- The auth token (not API key) is used in the env dict.
- If the base URL contains `/v1`, it is stripped before being combined with the SDK's own `/v1` append — preventing double-slash URLs.

### Gemini
`TestGeminiAdapter` checks model config and auth error formatting.

### LiteLLM
`TestLiteLLMAdapter` covers the most complex adapter:
- In native mode (no `base_url`), model strings are passed directly to LiteLLM.
- In proxy mode (with `base_url`), an OpenAI-compat client is used and the model string is wrapped differently.
- `build_adk_model_native()` vs `build_agents_model_proxy_mode()` reflect the two code paths for Google ADK integration.
- Connection errors produce a formatted "check your LiteLLM proxy" message.

## LLMClient Delegation

`TestLLMClientDelegation` verifies that `LLMClient` correctly delegates to adapter methods:

- `to_sdk_env()` calls the adapter's `build_env_dict()` for each provider.
- `format_api_error()` delegates to the adapter's error formatter.
- Unknown provider falls back gracefully rather than crashing.

## Known Gaps

- No integration test that actually calls a provider endpoint (all tests mock the network).
- Gemini adapter tests do not cover the proxy/base_url code path.
- No test for provider adapter selection when the settings object has conflicting values (e.g., both `anthropic_model` and `openai_model` set).