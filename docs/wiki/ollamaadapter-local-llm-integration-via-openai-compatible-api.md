---
{
  "title": "OllamaAdapter: Local LLM Integration via OpenAI-Compatible API",
  "summary": "`OllamaAdapter` connects PocketPaw to locally running Ollama instances, using `\"ollama\"` as a placeholder API key to satisfy client SDK validation while pointing both OpenAI and Anthropic client builders at Ollama's OpenAI-compatible HTTP endpoint. Error messages distinguish between model-not-found and service-not-running failures to give users actionable guidance.",
  "concepts": [
    "OllamaAdapter",
    "Ollama",
    "local LLM",
    "AsyncOpenAI",
    "AsyncAnthropic",
    "api_key placeholder",
    "OpenAI-compatible",
    "error differentiation",
    "offline operation"
  ],
  "categories": [
    "LLM integration",
    "Ollama",
    "provider adapter",
    "local inference"
  ],
  "source_docs": [
    "430295dfedf81774"
  ],
  "backlinks": null,
  "word_count": 393,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Ollama runs large language models locally and exposes an OpenAI-compatible REST API at `http://localhost:11434/v1`. `OllamaAdapter` integrates this into PocketPaw's provider system, enabling fully offline operation without any cloud API keys.

## The `"ollama"` API Key Placeholder

`AsyncOpenAI` and `AsyncAnthropic` require a non-empty `api_key` argument — passing `None` raises `AuthenticationError` at construction time. Ollama's local server ignores the API key entirely, but the client SDKs enforce its presence. Using `"ollama"` as the placeholder is both correct (it passes validation) and self-documenting (it identifies why a non-secret value is used here).

## OpenAI Client Construction

```python
def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
    from openai import AsyncOpenAI
    host = config.base_url or "http://localhost:11434"
    return AsyncOpenAI(
        base_url=f"{host.rstrip('/')}/v1",
        api_key="ollama",
        timeout=kwargs.get("timeout", 120.0),
        max_retries=kwargs.get("max_retries", 1),
    )
```

The `/v1` suffix is appended to `ollama_host` (stripped of trailing slashes). Ollama's OpenAI-compatible endpoint lives at `/v1/chat/completions`, consistent with the OpenAI spec. The 120s timeout is generous — local model inference, especially for larger models like `llama3.2:70b`, can take well over 60 seconds on CPU hardware.

## Anthropic Client Construction

`build_anthropic_client()` points `AsyncAnthropic` at Ollama's base host (without `/v1`), because the Anthropic SDK appends its own path. This is a subtle distinction from the OpenAI client, which needs `/v1` explicitly. Mixing these up would produce 404 errors on every request.

## Environment Variable Setup

```python
def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": config.base_url or "http://localhost:11434",
        "ANTHROPIC_API_KEY": "ollama",
    }
```

These env vars enable subprocess-based backends (Claude Agent SDK CLI mode) to route through Ollama. The `ANTHROPIC_AUTH_TOKEN` is intentionally absent — Ollama ignores authentication headers.

## Error Differentiation

`format_error()` checks for `"not_found"` in the raw error to detect model-not-found vs. connection-refused scenarios. A model-not-found error means Ollama is running but the requested model hasn't been pulled (`ollama pull llama3.2`). A connection-refused error means Ollama itself is not running. Giving different guidance for each prevents users from trying to start Ollama when they actually need to pull a model.

## Known Gaps

- **No Ollama health check before client creation**: the adapter constructs the client without verifying Ollama is running. A quick `GET /` health check in `resolve_config()` would surface connection issues at config time rather than at first inference.
- **`max_retries=1`**: local inference rarely benefits from retries (a slow model doesn't become faster on retry), but OOM kills or Ollama restarts during inference would benefit from one retry.