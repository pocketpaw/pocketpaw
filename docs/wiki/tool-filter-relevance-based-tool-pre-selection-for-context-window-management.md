---
{
  "title": "Tool Filter: Relevance-Based Tool Pre-Selection for Context Window Management",
  "summary": "The tool filter (`tools/filter.py`) limits the number of tools injected into an LLM context window by scoring tools against the current user message and always pinning a core set. Tests cover passthrough below the cap, max-tools truncation, always-include pinning semantics, relevance scoring, empty message handling, pinned slot accounting, and the tokenizer helper.",
  "concepts": [
    "filter_tools",
    "ALWAYS_INCLUDE",
    "context_window",
    "tool_selection",
    "relevance_scoring",
    "tokenizer",
    "max_tools",
    "pinned_tools",
    "BM25",
    "BaseTool"
  ],
  "categories": [
    "tool-system",
    "context-management",
    "testing",
    "test"
  ],
  "source_docs": [
    "0c56b1f23de8220a"
  ],
  "backlinks": null,
  "word_count": 513,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Large tool registries create a practical problem: sending all tool definitions to an LLM wastes context tokens and can degrade model performance. The `filter_tools` function solves this by selecting at most `max_tools` tools per turn, using a simple BM25-style token-overlap score to prefer tools relevant to the current user message.

## Why Tool Pre-Filtering Matters

PocketPaw can register dozens of tools across memory, filesystem, browser, email, calendar, and system categories. Injecting all of them in every turn:
- Consumes significant context window space (each tool schema adds tokens)
- Can confuse models into considering irrelevant tools
- Increases cost per call

The filter provides a lightweight, zero-latency selection step that happens before any LLM call.

## Passthrough Below the Cap

When the registered tool count is at or below `max_tools`, the function returns the list unchanged. This avoids unnecessary work and preserves ordering for small registries:

```python
def test_returns_all_tools_when_under_limit(self) -> None:
    tools = _make_tools(10)
    result = filter_tools("anything", tools, max_tools=15)
    assert result == tools
```

## Always-Include Pinning

A `frozenset` called `ALWAYS_INCLUDE` defines tools that appear in every context regardless of relevance score. This set typically contains core tools like `remember`, `recall`, and `web_search` — tools that are useful for almost any task. Pinned tools do not consume scored slots: if 5 tools are pinned and `max_tools=15`, there are still 10 scored slots available for relevance-matched tools.

```python
def test_pinned_dont_count_against_scored(self) -> None:
    pinned_names = list(ALWAYS_INCLUDE)[:5]
    # ... 5 pinned + 10 scored = 15 total
    result = filter_tools("some query", all_tools, max_tools=15)
    assert len(result) == 15
```

Callers can override `ALWAYS_INCLUDE` for specific invocations via the `always_include` parameter, enabling per-session or per-turn customization without modifying the global default.

## Relevance Scoring

Scoring uses token overlap: both the user message and each tool's name/description are tokenized, and a tool scores higher if more of its tokens appear in the message. The tokenizer normalizes to lowercase and strips punctuation. A message about "send an email to Alice" will rank `send_email` (with tokens `send`, `email`) above `get_weather` or `calculator`.

## Empty Message Handling

When the user message is empty (e.g., at session start), there is no signal to score against. The function returns the first `max_tools` tools in registration order rather than attempting to score against an empty query, which would assign all tools a score of zero and produce undefined ordering.

```python
def test_empty_message_returns_max(self) -> None:
    tools = _make_tools(25)
    result = filter_tools("", tools, max_tools=10)
    assert result == tools[:10]
```

## Tokenizer Behavior

The `_tokenize` helper is tested independently to ensure correctness:
- Strips punctuation (commas, exclamation marks, apostrophes)
- Lowercases all tokens
- Returns an empty set for empty input
- Includes numeric tokens (e.g., `"gpt4"` and `"3"` from `"GPT4 model version 3"`)

These properties are important for fair scoring — a user typing `"Send Email!"` should still match the `send_email` tool.

## Known Gaps

No TODOs or FIXMEs are present. The scoring algorithm is simple token overlap with no TF-IDF or other weighting — a tool with a very generic description could score artificially high if its tokens happen to appear frequently in messages.