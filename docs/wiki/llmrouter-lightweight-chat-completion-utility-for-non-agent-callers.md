---
{
  "title": "LLMRouter: Lightweight Chat Completion Utility for Non-Agent Callers",
  "summary": "`LLMRouter` is a simple, stateful chat utility for one-off completions — not used by agent backends. It auto-detects the best available backend (Ollama first, then cloud providers), maintains a conversation history list, and provides `clear_history()` for resetting between sessions. Its documented limitations (no streaming, no token counting, unbounded history) make its intended scope explicit.",
  "concepts": [
    "LLMRouter",
    "chat completion",
    "auto-detection",
    "Ollama",
    "conversation history",
    "clear_history",
    "non-streaming",
    "Guardian AI",
    "lightweight utility"
  ],
  "categories": [
    "LLM integration",
    "utilities",
    "chat completion",
    "diagnostics"
  ],
  "source_docs": [
    "b992baaecba9e2bf"
  ],
  "backlinks": null,
  "word_count": 391,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Most LLM interactions in PocketPaw go through agent backends (Claude Agent SDK, OpenAI Agents, Google ADK), each with their own sophisticated client setup. `LLMRouter` exists for everything else: lightweight diagnostic tools, Guardian AI checks, audit scripts, and future `doctor` commands that need a simple question-answer loop without tool use.

## Auto-Detection Logic

```python
async def _check_ollama(self) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            ...
    except Exception:
        return False
```

`LLMRouter` checks for a running Ollama instance first. Ollama is preferred because it's free and local — no API costs for internal diagnostic queries. The 2-second timeout prevents startup delay when Ollama is not installed; a longer timeout would make the first `chat()` call feel sluggish.

If Ollama is unavailable, the router falls through to cloud providers based on settings (Anthropic, then OpenAI-compatible, then others).

## Conversation History

```python
self.conversation_history: list[dict] = []
```

The router accumulates messages in `conversation_history` as a list of `{role, content}` dicts, matching the OpenAI message format. This enables multi-turn conversations without the caller managing history manually. The `clear_history()` method resets to an empty list — necessary when starting a new diagnostic session or switching topics.

The history is unbounded, which the module doc acknowledges as a limitation. For production use, callers must call `clear_history()` periodically or the context window will eventually be exceeded.

## Provider-Specific Chat Methods

`_chat_ollama()`, `_chat_openai()`, `_chat_anthropic()`, and `_chat_openai_compat()` each handle the request/response format for their respective providers. This avoids provider detection scattered across a single `chat()` method body.

`chat()` dispatches to the right private method based on `_available_backend`, which is cached after the first call so Ollama availability is not re-checked on every message.

## No Streaming

`LLMRouter` returns the full response as a string. This is appropriate for its use cases (short diagnostic queries, Guardian AI checks) but makes it unsuitable for interactive chat where users expect incremental output. Agent backends handle streaming natively.

## Known Gaps

- **Unbounded conversation history**: acknowledged in the module doc. A `max_history_turns` parameter would prevent silent context window overflows.
- **No token counting**: the router has no visibility into how many tokens the history consumes. Callers cannot proactively trim history before it exceeds provider limits.
- **`_available_backend` caching**: if Ollama stops running mid-session, the cached `_available_backend` value does not update. Subsequent calls will fail rather than falling through to a cloud provider.