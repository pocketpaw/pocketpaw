---
{
  "title": "Deep Agents Backend Tests: LangChain Integration, Provider Parsing, Content Extraction, and Registry",
  "summary": "This test module validates the `DeepAgentsBackend` — PocketPaw's LangChain-based agent backend — through mocks, requiring no real LangChain or LangGraph installation. It covers static metadata, the `provider:model` string parsing convention, LangGraph `Overwrite` object unwrapping, content extraction from mixed message formats, graceful degradation when the SDK is absent, and backend registry integration.",
  "concepts": [
    "DeepAgentsBackend",
    "LangChain",
    "LangGraph",
    "Overwrite unwrapping",
    "provider:model format",
    "BackendInfo",
    "beta flag",
    "install_hint",
    "content extraction",
    "graceful degradation",
    "custom tools",
    "backend registry",
    "STREAMING capability"
  ],
  "categories": [
    "agent runtime",
    "testing",
    "backend adapters",
    "LangChain integration",
    "test"
  ],
  "source_docs": [
    "1e0a080771543d2a"
  ],
  "backlinks": null,
  "word_count": 574,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `DeepAgentsBackend` wraps LangChain/LangGraph to support multi-provider agentic workflows with tool use. Because LangChain is an optional dependency installed via `pocketpaw[deep-agents]`, the backend must handle missing imports gracefully, and all tests mock the LangChain layer to keep CI fast and dependency-free.

## Static Metadata (`TestDeepAgentsBackendInfo`)

The backend's `info()` method returns a `BackendInfo` object consumed by the registry and configuration UI:
- `name == "deep_agents"` (the registry key)
- `display_name == "Deep Agents (LangChain)"` (shown in UI)
- `beta=True` — signals to users that the backend is not yet production-stable
- `install_hint` provides the pip spec (`pocketpaw[deep-agents]`) and the import to verify (`deepagents`), enabling the UI to show an actionable installation command when the backend is unavailable
- Capabilities include `STREAMING`, `TOOLS`, `MULTI_TURN`, and `CUSTOM_SYSTEM_PROMPT` — the same set as other full-featured backends

## Provider:Model Parsing (`TestDeepAgentsProviderParsing`)

The `deep_agents_model` setting uses a `provider:model` colon-separated format (e.g., `"anthropic:claude-sonnet-4-6"`, `"openai:gpt-4o"`, `"ollama:llama3.2"`, `"google_genai:gemini-2.0-flash"`, `"litellm:gpt-4"`). The `_parse_provider_model()` method splits on the first colon.

Two edge cases are tested:
- `test_parse_model_only_defaults_to_anthropic`: a model string with no colon defaults the provider to `"anthropic"`, preserving backward compatibility with configurations written before the multi-provider format was introduced.
- `test_parse_empty_model_defaults`: an empty model string returns a sensible default provider and model rather than raising.

The parsing tests use a real `Settings` object with the `deep_agents_model` field set, ensuring the parsing logic interacts correctly with Pydantic's field coercion.

## LangGraph Overwrite Unwrapping (`TestDeepAgentsUnwrap`)

LangGraph's state management sometimes returns `Overwrite` wrapper objects rather than plain values. `_unwrap` extracts the underlying value from an `Overwrite` and passes plain values through unchanged. `test_unwrap_none` is tested separately because `None` is a valid unwrapped value that must be distinguished from "no value".

Without this helper, LangGraph `Overwrite` objects would be stringified as `<Overwrite value=...>` in the agent's output, producing garbage in the conversation.

## Content Extraction (`TestDeepAgentsContentExtraction`)

LangChain messages can carry content as a plain string, a list of text-block dicts (`{"type": "text", "text": "..."}` from Anthropic), or a mixed list. `_extract_content_text` normalizes all forms to a plain string. Tests cover:
- Plain string passthrough
- List of text-block dicts (Anthropic multi-block format)
- Mixed list (text blocks and non-text blocks — non-text is ignored)
- List of plain strings (OpenAI function-call format)
- Empty content → empty string

## Initialization and Graceful Degradation (`TestDeepAgentsBackendInit`)

`test_custom_tools_cached` verifies that the custom tool list is built once at init time and cached, preventing repeated expensive tool discovery on every `run()` call.

`test_custom_tools_graceful_degradation` simulates a tool discovery failure (e.g., a broken tool plugin) and asserts the backend initializes with an empty tool list rather than raising. This prevents a single broken tool from making the entire backend unusable.

## Run Behavior and Status (`TestDeepAgentsBackendRun`)

`test_run_sdk_unavailable_yields_error` asserts that when LangChain is not installed, `run()` yields a single error event rather than raising `ImportError` — maintaining the async generator contract so callers don't need special-case handling.

`test_get_status_shows_resolved_provider` verifies that `get_status()` includes the resolved provider name (after `_parse_provider_model()`), enabling the dashboard to show which LLM provider is actually in use.

## Registry Integration (`TestDeepAgentsRegistry`)

The backend must be registered under `"deep_agents"` in PocketPaw's backend registry and loadable by class name. These tests catch regressions where the backend's `@register_backend` decorator or its registration call is accidentally removed.

## Known Gaps

The `beta=True` flag is tested but there is no test asserting that the dashboard UI surfaces a beta warning when this flag is set. That behavior lives in the frontend layer, which is tested separately.