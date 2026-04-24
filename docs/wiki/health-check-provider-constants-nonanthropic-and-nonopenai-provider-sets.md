---
{
  "title": "Health Check Provider Constants — NON_ANTHROPIC and NON_OPENAI Provider Sets",
  "summary": "This module defines two tuple constants used across the health check subsystem to identify which LLM providers do not require Anthropic or OpenAI API keys, enabling the connectivity router to correctly dispatch provider-specific reachability checks. Centralising these sets prevents scattered string literals and makes adding a new provider a single-file change.",
  "concepts": [
    "provider constants",
    "NON_ANTHROPIC_PROVIDERS",
    "NON_OPENAI_PROVIDERS",
    "connectivity routing",
    "health checks",
    "LLM providers",
    "Ollama",
    "OpenRouter",
    "LiteLLM",
    "Gemini",
    "provider registry"
  ],
  "categories": [
    "health monitoring",
    "configuration"
  ],
  "source_docs": [
    "eb53b0306610a864"
  ],
  "backlinks": null,
  "word_count": 378,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`constants.py` is a small but load-bearing module in PocketPaw's health subsystem. It defines exactly two constants:

```python
NON_ANTHROPIC_PROVIDERS = ("ollama", "openai_compatible", "gemini", "litellm", "openrouter")
NON_OPENAI_PROVIDERS = ("ollama", "openai_compatible", "litellm", "openrouter")
```

These tuples answer the question: *when a user configures a non-default provider on a backend, which providers do not need an Anthropic (or OpenAI) API key?*

## Why These Constants Exist

PocketPaw supports multiple agent backends, each with a default provider that ships with its own required API key:

- `claude_agent_sdk` defaults to Anthropic — but users can swap in OpenRouter, Ollama, or LiteLLM as the underlying model provider.
- `openai_agents` defaults to OpenAI — but similarly supports OpenRouter, LiteLLM, and others.

Without these constants, the connectivity checks in `connectivity.py` would need to repeat the same provider lists inline. Centralising them here means that if a new alternative provider (e.g., `vllm`) is supported, only this file needs updating — the routing logic in `connectivity.py` picks up the change automatically via the `in NON_ANTHROPIC_PROVIDERS` membership test.

## Why Tuples, Not Sets

The constants are plain tuples rather than `frozenset`. For the small cardinality involved (five elements or fewer) and the read-only nature of these constants, tuples are idiomatic Python and have negligible performance difference. The `in` operator on a tuple is O(n), but with n ≤ 5 this is faster in practice than the hash overhead of a frozenset for cold lookups.

## Gemini in NON_ANTHROPIC but Not NON_OPENAI

`gemini` appears in `NON_ANTHROPIC_PROVIDERS` but not in `NON_OPENAI_PROVIDERS`. This reflects the real routing topology: Gemini can be used as an alternative provider under `claude_agent_sdk` (via a bridge), but there is no corresponding Gemini-as-alternate-provider path for the `openai_agents` backend.

## Usage Pattern

```python
from pocketpaw.health.checks.constants import NON_ANTHROPIC_PROVIDERS, NON_OPENAI_PROVIDERS

if provider in NON_ANTHROPIC_PROVIDERS:
    return await _check_alt_provider_reachable(settings, provider)
return await _check_anthropic_reachable(settings)
```

The constants are imported only in `connectivity.py` today, but the module is designed to be shared across any health check that needs to reason about provider identity.

## Known Gaps

- The constants are not derived from any canonical provider registry — they are manually maintained tuples. If a new provider is added to `settings.py` but not to these constants, the health check will silently fall through to the default provider path, potentially giving a misleading connectivity result.
