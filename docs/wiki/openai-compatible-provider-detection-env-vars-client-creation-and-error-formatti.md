---
{
  "title": "OpenAI-Compatible Provider: Detection, Env Vars, Client Creation, and Error Formatting",
  "summary": "Tests for PocketPaw's OpenAI-compatible endpoint support in LLMClient, enabling any OpenAI-compatible API (LiteLLM, Groq, local proxies) as the LLM backend. Covers provider detection, model resolution, environment variable construction for subprocess mode, Anthropic client creation with custom base URL, and structured error formatting for connection and authentication failures.",
  "concepts": [
    "openai_compatible",
    "LLMClient",
    "base URL",
    "provider detection",
    "env vars",
    "error formatting",
    "LiteLLM",
    "Groq",
    "subprocess mode",
    "smart routing",
    "authentication",
    "model not found"
  ],
  "categories": [
    "LLM providers",
    "OpenAI compatible",
    "testing",
    "error handling",
    "test"
  ],
  "source_docs": [
    "c31c515f1c681aeb"
  ],
  "backlinks": null,
  "word_count": 522,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `openai_compatible` provider type lets PocketPaw connect to any server that speaks the OpenAI API format — LiteLLM proxies, Groq, Together AI, or local inference servers. Because these services share an API shape with OpenAI but have different authentication and base URLs, the `LLMClient` needs careful provider detection and configuration. This test file validates every configuration branch.

## Provider Detection (`TestLLMClientOpenAICompatible`)

- `test_provider_detection`: When `llm_provider="openai_compatible"`, the resolved client sets `is_openai_compatible=True` and clears `is_ollama` and `is_anthropic`. The mutual exclusivity is important — downstream code switches on these flags.
- `test_model_resolved`: The model name comes from `openai_compatible_model`, not the global `model` setting. This allows using GPT-4o or a custom model name without affecting other providers.
- `test_base_url_stored`: The `openai_compatible_base_url` is stored on the client object for use when constructing API calls and subprocess env vars.
- `test_api_key_carried` / `test_api_key_optional`: The API key is optional — some self-hosted servers accept any key or no key at all. When present, it must be carried to the client; when absent, the client must not crash.

## Environment Variables (`TestLLMClientOpenAICompatibleEnv`)

When running the LLM via the Claude SDK subprocess mode, credentials are passed as environment variables:

- `test_env_vars_with_key`: With a key, both `OPENAI_API_KEY` and `OPENAI_API_BASE` must be set. The base URL override is critical — without it, the subprocess would send requests to `api.openai.com` instead of the local proxy.
- `test_env_vars_without_key`: Without a key, a placeholder or empty value is used. The subprocess must still receive `OPENAI_API_BASE` so it knows where to connect.

## Client Creation (`TestLLMClientOpenAICompatibleClient`)

- `test_creates_client_with_base_url`: The `Anthropic` client (used as the underlying HTTP client in Claude SDK mode) is initialized with `base_url` pointing to the compatible endpoint.
- `test_creates_client_without_key`: Client creation succeeds even without an API key, passing `None` or empty string rather than raising.

## Error Formatting (`TestLLMClientOpenAICompatibleErrors`)

OpenAI-compatible servers return different error shapes than Anthropic's API. The client must normalize these into messages the agent can act on:

- `test_connection_error`: Network failures produce "cannot connect to `{base_url}`" messages, helping users identify misconfigured base URLs.
- `test_generic_error`: Unexpected errors include the original error text but are wrapped in a structured format.
- `test_model_not_found_via_stderr`: When the subprocess fails with "model not found" in stderr, that specific message is surfaced rather than a generic error. This directs users to `ollama pull` or a model name correction.
- `test_stderr_surfaced_in_generic_error`: Any stderr output from a failed subprocess is included in the error message, preserving debugging information that would otherwise be swallowed.
- `test_auth_error`: 401 responses produce an authentication-specific message pointing to the API key configuration.

## Smart Routing Behavior

- `test_smart_routing_skipped`: Like Ollama, OpenAI-compatible endpoints are served by a single configured model, so tier-based routing is bypassed.
- `test_smart_routing_enabled_for_anthropic`: Confirms the skip only applies to this provider and does not accidentally disable routing for Anthropic.

## CLI Health Check (`TestCheckOpenAICompatible`)

- `test_empty_base_url_...`: Without a configured base URL, the check command returns an error immediately without attempting a connection.

## Known Gaps

No TODOs in this file. The error formatting tests mock the underlying Anthropic client rather than a real OpenAI-compatible server, so error message format differences between real providers (Groq vs LiteLLM) are not captured.
