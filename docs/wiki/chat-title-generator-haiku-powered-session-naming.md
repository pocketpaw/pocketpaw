---
{
  "title": "Chat Title Generator — Haiku-Powered Session Naming",
  "summary": "`titler.py` generates concise (6 words max, Title Case) chat titles from the first user message using a Claude Haiku-class model. Title generation is entirely best-effort: any failure is silently logged and returns `None` rather than disrupting the chat flow.",
  "concepts": [
    "chat title generation",
    "Claude Haiku",
    "AsyncAnthropic",
    "best-effort",
    "MAX_TOKENS",
    "MAX_INPUT_CHARS",
    "session_titled",
    "deferred import",
    "title normalisation"
  ],
  "categories": [
    "Memory System",
    "User Experience"
  ],
  "source_docs": [
    "75619b942aa83fe6"
  ],
  "backlinks": null,
  "word_count": 529,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a new conversation starts, PocketPaw needs a human-readable title for the session list in the dashboard. Asking the user to name their conversation adds friction. Generating the title automatically from the first message is friendlier.

`titler.py` handles only the generation step. Persistence (saving the title to the session) and event emission (`session_titled` SystemEvent) are the caller's responsibility. This separation means the titler can be tested in isolation and reused in different contexts.

## The Prompt

```python
_PROMPT = (
    "Write a concise chat title (max 6 words, Title Case, no quotes, no"
    " trailing punctuation) that captures the subject of this user message.\n\n"
    "Message:\n{message}\n\nTitle:"
)
_MAX_TOKENS = 24
```

`_MAX_TOKENS = 24` is intentionally small — a 6-word title needs at most ~10 tokens. The tight limit prevents the model from generating a verbose response and caps cost per title call to a fraction of a cent.

`_MAX_INPUT_CHARS = 2000` truncates very long first messages before sending to the API, preventing runaway token costs when users paste large blocks of text as their first message.

## Model Choice

The function accepts `model` as a parameter. In practice, the caller passes a Haiku-class model (fastest, cheapest) because title generation is latency-sensitive (it happens during the first response) and accuracy requirements are low.

## Error Handling Strategy

```python
try:
    from anthropic import AsyncAnthropic
except ImportError:
    logger.debug("anthropic SDK not installed; skipping title generation")
    return None
```

The SDK import is deferred to inside the function. This means `titler.py` can be imported in environments where `anthropic` is not installed without crashing. All API errors are caught and logged at `debug` level because title generation failure is not actionable by the operator.

## Post-Processing

The raw model response is stripped of leading/trailing quotes, apostrophes, and trailing punctuation to normalise output across model versions that may vary in their adherence to the prompt instructions.

## Separation of Concerns

A deliberate design choice is that `generate_title` does not persist the title and does not emit events. The caller (typically the session creation path in `MemoryManager`) is responsible for both. This means the titler can be used in contexts where persistence is handled differently — for example, in a test that generates titles without writing to disk, or in a future batch titling job that retitles old sessions.

The `session_titled` SystemEvent is emitted by the caller after saving, which triggers a WebSocket push to the dashboard so the sidebar updates the session name in real time without requiring a page reload. The titler itself knows nothing about WebSocket or event buses.

## Word Count Cap

After generation, the response is split by spaces and capped at 8 words to handle model over-generation. Even though the prompt says 'max 6 words', models occasionally produce longer titles. The cap is set slightly above 6 to allow for punctuation tokens that do not count as words but still split on spaces.

## Known Gaps

No caching: if the same message triggers multiple title generation calls (e.g., due to retries), each call hits the API independently. No language detection: the title is always generated in the language the model chooses, which may differ from the user's input language.