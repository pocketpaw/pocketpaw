---
{
  "title": "LLM Connectivity Health Checks — Provider-Aware Reachability",
  "summary": "This module implements async health checks that verify whether the configured LLM API endpoint is reachable, routing to the correct check function based on the active backend and provider. It covers Anthropic, OpenAI, Google AI, Ollama, OpenRouter, LiteLLM, and generic OpenAI-compatible endpoints, each with a 5-second timeout and structured pass/warn/critical results.",
  "concepts": [
    "health checks",
    "LLM reachability",
    "connectivity",
    "provider routing",
    "Anthropic API",
    "OpenRouter",
    "Ollama",
    "LiteLLM",
    "OpenAI",
    "Google AI",
    "httpx",
    "HealthCheckResult",
    "async checks",
    "5-second timeout",
    "API key validation"
  ],
  "categories": [
    "health monitoring",
    "LLM backends"
  ],
  "source_docs": [
    "d31f78d1c95dd288"
  ],
  "backlinks": null,
  "word_count": 528,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`connectivity.py` is PocketPaw's LLM reachability subsystem. Its job is answering a deceptively simple question: *can the agent actually reach the AI API it is configured to use?* Getting this right is non-trivial because PocketPaw supports three distinct agent backends (`claude_agent_sdk`, `google_adk`, `openai_agents`) and each backend can be pointed at multiple provider implementations. A single generic ping would not work — the correct endpoint differs per provider.

## Routing Architecture

The public entry point is `check_llm_reachable()`. It reads `settings.agent_backend` and branches into provider-specific checks:

- **claude_agent_sdk** defaults to Anthropic but can be overridden with `claude_sdk_provider`. Non-Anthropic providers (OpenRouter, LiteLLM, Ollama, OpenAI-compatible, Gemini) are routed through `_check_alt_provider_reachable()`.
- **google_adk** defaults to Google's Generative Language API but can be re-routed to LiteLLM.
- **openai_agents** defaults to OpenAI but can be pointed at OpenRouter, LiteLLM, Ollama, or a generic OpenAI-compatible endpoint.

The constants `NON_ANTHROPIC_PROVIDERS` and `NON_OPENAI_PROVIDERS` (imported from `constants.py`) drive these routing decisions, keeping the branching logic DRY and central.

## Provider-Specific Checks

Each check function follows the same contract: perform one HTTP request with a 5-second timeout, then return a `HealthCheckResult` with status `ok`, `warning`, or `critical`.

```python
async def _check_anthropic_reachable(settings) -> HealthCheckResult:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
```

The 5-second timeout is intentional: health checks run at startup and should never block the application from loading. A slow or hung network call without a timeout would freeze the UI.

The checks distinguish between network failures (no route to host → `critical`), authentication failures (HTTP 401/403 → `critical` with a "check your key" hint), unexpected HTTP codes (`warning`), and success (`ok`). This three-tier severity model lets the dashboard surface the right remediation action.

## Missing API Key Handling

Before making any network call, each check verifies that an API key exists. An absent key yields a `warning` (not `critical`) because connectivity cannot be tested — but the agent is not necessarily broken if the key is provided elsewhere at runtime. This avoids false alarms during first-run configuration.

## Ollama: Local Server Ping

Ollama is self-hosted, so the check pings `/api/tags` (the list-models endpoint) rather than a cloud API. On success it reports how many models are available, which is useful context for operators. On failure it provides the actionable fix hint "Start Ollama with: `ollama serve`".

## Fallback Warning for Unknown Providers

If neither the backend nor the provider matches any known combination, the function returns a `warning` status with the message "Connectivity check not implemented for `{provider}`". This prevents a hard crash when a new provider is added to settings before a matching check function is written — the health system degrades gracefully rather than failing loudly.

## Known Gaps

- There is no automatic retry on transient failures. A single network hiccup at startup will mark the check as `critical` until the next health run.
- `google_adk` only has a fallthrough to LiteLLM as an alternate provider. Other non-Google providers that the ADK might support are not covered.
- The `_check_openai_compat_reachable` function reads `openai_compatible_base_url` from settings but does not validate the URL format before attempting the connection, which could produce confusing errors for misconfigured URLs.
