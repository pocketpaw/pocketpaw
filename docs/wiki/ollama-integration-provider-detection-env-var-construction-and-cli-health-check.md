---
{
  "title": "Ollama Integration: Provider Detection, Env Var Construction, and CLI Health Check",
  "summary": "Tests for PocketPaw's Ollama provider support, verifying that LLMClient correctly detects Ollama from settings, constructs the right subprocess environment variables, and skips smart model routing for local models. Also covers the `--check-ollama` CLI command that validates server reachability and model availability.",
  "concepts": [
    "Ollama",
    "provider detection",
    "LLMClient",
    "resolve_llm_client",
    "auto provider",
    "environment variables",
    "smart routing",
    "CLI health check",
    "local LLM",
    "subprocess",
    "model routing skip"
  ],
  "categories": [
    "LLM providers",
    "Ollama",
    "testing",
    "CLI",
    "test"
  ],
  "source_docs": [
    "8720483cc1c85a96"
  ],
  "backlinks": null,
  "word_count": 492,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Ollama enables PocketPaw to run entirely offline using local LLMs like Mistral or LLaMA. Because Ollama shares the `LLMClient` code path with Anthropic and OpenAI-compatible providers, the tests focus on provider detection branching — ensuring the client correctly identifies Ollama and configures itself differently from cloud providers.

## Provider Detection (`TestClaudeSDKOllamaLogic`)

Rather than mocking the full Claude SDK initialization (which is complex), these tests exercise `resolve_llm_client()` directly with different `Settings` objects:

- `test_ollama_provider_detection`: When `llm_provider="ollama"`, the resolved client has `is_ollama=True`. This flag gates Ollama-specific code paths throughout the application.
- `test_auto_without_key_detects_ollama`: The `"auto"` provider does smart detection: no `anthropic_api_key` + Ollama host configured → falls back to Ollama. This lets users run PocketPaw without any configuration — it just works if Ollama is running locally.
- `test_auto_with_key_uses_anthropic`: Conversely, when an Anthropic API key is present, `"auto"` chooses Anthropic over Ollama. The key is treated as an explicit signal of intent.

## Environment Variable Construction

When the LLM runs as a subprocess (via Claude Code SDK), it receives provider credentials through environment variables rather than function arguments. Tests verify the exact env var names and values:

- `test_ollama_env_vars_construction`: Ollama requires `OLLAMA_HOST` and `OLLAMA_MODEL` — not `ANTHROPIC_API_KEY`. Sending the wrong env vars causes the subprocess to fail to connect.
- `test_anthropic_env_vars_construction`: Anthropic requires `ANTHROPIC_API_KEY`. The two sets must not overlap — leaking `ANTHROPIC_API_KEY` into an Ollama subprocess would expose credentials unnecessarily.

## Smart Routing Behavior

PocketPaw's model router selects between Haiku/Sonnet/Opus tiers based on task complexity. For Ollama, this makes no sense — there is only one locally configured model:

- `test_smart_routing_skipped_for_ollama`: When `is_ollama=True`, the model router does not apply tier selection. Without this skip, the router might override the configured `ollama_model` with an Anthropic model name, causing the Ollama client to request a non-existent model.
- `test_smart_routing_enabled_for_anthropic`: Confirms smart routing remains active for Anthropic, so the existing optimization is not accidentally disabled.

## CLI Health Check (`TestCheckOllama`)

The `--check-ollama` CLI command lets operators verify their Ollama setup before running PocketPaw:

- `test_server_unreachable_returns_1`: If Ollama is not running (connection refused), the command exits with code 1 and prints a user-friendly error. Exit code 1 is standard for "check failed" in shell scripts.
- `test_server_reachable_model_missing`: If Ollama is running but the configured model has not been pulled (`ollama pull mistral:7b`), the command reports the model is missing with instructions. This prevents a confusing runtime failure where the server accepts the connection but then fails on the first inference request.

## Design Notes

The `TestClaudeSDKOllamaLogic` class comment explicitly explains the testing strategy: "Instead of trying to mock the complex SDK initialization, we test the provider selection logic via `resolve_llm_client` directly." This is a deliberate design decision that trades implementation-coupled tests for logic-focused tests that survive SDK changes.

## Known Gaps

No TODOs in the file. The tests do not cover streaming responses from Ollama, which has different chunking behavior than the Anthropic SDK. Streaming correctness is assumed to be tested at the integration level.
