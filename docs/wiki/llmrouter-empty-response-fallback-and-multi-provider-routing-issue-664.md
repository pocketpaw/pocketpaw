---
{
  "title": "LLMRouter: Empty-Response Fallback and Multi-Provider Routing (Issue #664)",
  "summary": "The LLMRouter test suite focuses on a specific regression (issue #664) where an empty `choices` or `content` list from the upstream LLM API caused an unhandled `IndexError`, crashing the agent loop. Tests also validate backend auto-detection logic for all supported providers — OpenAI-compatible, OpenRouter, Gemini, LiteLLM — and ensure `chat()` routes each provider through the correct internal method.",
  "concepts": [
    "LLMRouter",
    "empty-response fallback",
    "IndexError",
    "issue #664",
    "backend detection",
    "OpenAI-compatible",
    "OpenRouter",
    "Gemini",
    "LiteLLM",
    "Ollama",
    "provider routing",
    "chat method"
  ],
  "categories": [
    "LLM integration",
    "error handling",
    "test"
  ],
  "source_docs": [
    "265d495127093cc6"
  ],
  "backlinks": null,
  "word_count": 456,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `LLMRouter` sits between the agent loop and the raw LLM SDK, handling provider detection and dispatching calls to the appropriate internal `_chat_*` method. Issue #664 identified a crash path: when the upstream API returns an empty `choices` list (OpenAI-compatible) or an empty `content` list (Anthropic), indexing into it with `[0]` raised `IndexError`, which propagated to the user as an unhandled exception. The fix introduces a fallback string instead.

## Empty-Response Fallback

The fallback string is defined as a module-level constant:

```python
FALLBACK = "I'm sorry, I received an empty response. Please try again."
```

Three test classes validate this:

**`TestChatOpenAIEmptyResponse`**: Mocks a response with `choices=[]` and asserts that `_chat_openai()` returns the fallback string without raising. The companion test `test_empty_choices_does_not_raise` uses `pytest.raises` to confirm no exception escapes.

**`TestChatAnthropicEmptyResponse`**: Same pattern for `_chat_anthropic()` with `content=[]`.

**`TestChatFallbackIntegration`**: End-to-end test through the public `chat()` method, verifying that the fallback surfaces correctly when the router dispatches to either backend. This matters because `chat()` catches some exceptions internally and re-raises others — the integration test confirms the fallback passes through without being caught or swallowed.

## Backend Detection

`_detect_backend()` inspects settings to select a provider string. Tests cover:

- **`openai_compatible`**: requires both `openai_compatible_base_url` and `openai_compatible_api_key`; missing URL falls back.
- **`openrouter`**: detected by `openrouter_api_key` or the OpenRouter-compatible key variant.
- **`gemini`**: detected by `gemini_api_key`.
- **`litellm`**: detected by `litellm_api_key`.
- **Auto priority**: `test_auto_selects_gemini_over_ollama` confirms Gemini wins over Ollama when both could apply; `test_auto_selects_openrouter_when_key_set` confirms OpenRouter wins when its key is present.

The auto-priority order matters for users who configure multiple backends: the router must pick the "most capable" or "most expensive" provider last in fallback order, ensuring Ollama (free, local) is a genuine last resort.

## OpenAI-Compatible Provider Routing

`TestChatOpenAICompatProviders` uses `pytest.mark.parametrize` across `["openai_compatible", "openrouter", "gemini", "litellm"]` to confirm that `chat()` routes all four through `_chat_openai_compat()` rather than `_chat_openai()`. This is important because `_chat_openai_compat()` sets the correct base URL and auth header for each provider, while `_chat_openai()` is the vanilla OpenAI path.

## Why This Fix Matters

Empty-response events are not rare: rate-limited requests, model overloads, and content-filter rejections can all return an empty choices array with a non-error HTTP status. Without the fallback, every such event would crash the agent loop and force the user to restart their session. The fallback converts a hard crash into a recoverable soft failure that the user can retry.

## Known Gaps

- The fallback string is hardcoded in the test module as `FALLBACK`. If the production code's fallback text changes, the test will fail for the wrong reason — the test should import the constant from the production module.
- There is no test for streaming responses with empty delta content, which is a distinct code path from non-streaming empty `choices`.