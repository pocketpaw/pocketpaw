---
{
  "title": "GeminiAdapter: Google AI via OpenAI-Compatible Endpoint",
  "summary": "`GeminiAdapter` routes Gemini API calls through Google's OpenAI-compatible endpoint, allowing PocketPaw to use the same `AsyncOpenAI` and `AsyncAnthropic` client patterns it uses for other providers. Both client builders point to `GEMINI_BASE_URL` and use `\"not-needed\"` as a placeholder API key fallback to satisfy SDK validation without passing a real credential.",
  "concepts": [
    "GeminiAdapter",
    "GEMINI_BASE_URL",
    "OpenAI-compatible endpoint",
    "AsyncOpenAI",
    "AsyncAnthropic",
    "Google AI",
    "api_key placeholder",
    "environment variables",
    "provider adapter"
  ],
  "categories": [
    "LLM integration",
    "Google Gemini",
    "provider adapter",
    "OpenAI compatibility"
  ],
  "source_docs": [
    "e674ab66055bd7a8"
  ],
  "backlinks": null,
  "word_count": 368,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Google's Gemini API exposes an OpenAI-compatible REST interface at `https://generativelanguage.googleapis.com/v1beta/openai/`. `GeminiAdapter` exploits this to integrate Gemini into PocketPaw's provider system without a dedicated Gemini SDK dependency.

## The OpenAI-Compatible Strategy

Rather than importing `google-generativeai` or `google-cloud-aiplatform`, `GeminiAdapter` wraps `AsyncOpenAI` pointed at `GEMINI_BASE_URL`. This means the same tool-calling, streaming, and message-format code paths used for OpenAI work transparently for Gemini. The trade-off is that Gemini-specific features (grounding, safety settings, multimodal native APIs) are not accessible through this adapter.

## Why `build_anthropic_client` Also Works

```python
def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(
        base_url=GEMINI_BASE_URL,
        api_key=config.api_key or "not-needed",
        ...
    )
```

Some agent backends (Claude Agent SDK) use `AsyncAnthropic` as their client type. By pointing `AsyncAnthropic` at `GEMINI_BASE_URL`, those backends can route through Gemini without code changes. This works because Anthropic's Python SDK respects `base_url` and speaks standard HTTP — at the wire level it becomes a regular OpenAI-compatible request.

## The `"not-needed"` Placeholder

Both client builders pass `api_key=config.api_key or "not-needed"`. The `AsyncOpenAI` and `AsyncAnthropic` constructors validate that `api_key` is a non-empty string — passing `None` raises immediately. When `google_api_key` is set, it flows through correctly. The `"not-needed"` fallback is a defensive measure that keeps the adapter functional in environments where the key will be injected via another mechanism (e.g., Application Default Credentials via gcloud).

## Environment Variable Env Dict

```python
def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
    env = {"ANTHROPIC_BASE_URL": GEMINI_BASE_URL}
    env["ANTHROPIC_API_KEY"] = config.api_key or "not-needed"
    return env
```

Setting `ANTHROPIC_BASE_URL` alongside `ANTHROPIC_API_KEY` ensures that subprocess-based backends (which read env vars rather than Python objects) also route through Gemini correctly.

## Error Handling

`format_error()` lowercases the combined exception string and stderr, then checks for auth-related keywords. On auth failures it surfaces a Settings-redirect message. For quota errors it suggests checking the Google AI Studio dashboard.

## Known Gaps

- **No Gemini-native SDK path**: features like grounding, search, and native multimodal inputs require the `google-generativeai` SDK, which this adapter does not use.
- **Longer default timeout (120s)**: Gemini can be slower than Anthropic's cloud under load. The 120s timeout is reasonable but not configurable per-call.
- **`max_retries=1`**: Gemini's API can return transient 503s. A single retry may not be enough for high-reliability workflows.