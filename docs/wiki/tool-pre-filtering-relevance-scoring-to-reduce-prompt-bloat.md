---
{
  "title": "Tool Pre-Filtering: Relevance Scoring to Reduce Prompt Bloat",
  "summary": "The tool filter module scores each registered tool against the current user message using token overlap, returning only the top-N most relevant tools to avoid overloading the LLM context window. A curated set of essential tools -- memory, search, and pocket management -- are always included regardless of relevance score.",
  "concepts": [
    "tool filtering",
    "token overlap",
    "prompt optimization",
    "ALWAYS_INCLUDE",
    "BaseTool",
    "filter_tools",
    "relevance scoring",
    "agent loop",
    "LLM context",
    "tool registry"
  ],
  "categories": [
    "tools",
    "agent runtime",
    "performance optimization"
  ],
  "source_docs": [
    "3628239ecab62cca"
  ],
  "backlinks": null,
  "word_count": 579,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

As PocketPaw's tool registry grows, passing every registered tool definition to the LLM on every request becomes increasingly expensive. Each tool definition occupies tokens in the prompt; a large registry could consume thousands of tokens before the user's message even gets processed. The `filter.py` module solves this problem with a lightweight pre-filtering step that runs before the tool list reaches the routing loop.

## How It Works

The filtering algorithm uses **token overlap** as a proxy for relevance. Both the user message and each tool's combined name and description are tokenized into sets of lowercase alphanumeric strings (punctuation stripped). The intersection size between these two sets is used as a relevance score -- higher overlap means the tool is more likely to be useful for this particular message.

```python
def _tokenize(text: str) -> set[str]:
    """Split text into lowercase alphanumeric tokens, stripping punctuation."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))
```

The `filter_tools()` function accepts a message, the full tool list, and two optional keyword arguments: `always_include` (a frozenset of tool names that are pinned regardless of score) and `max_tools` (default 15). The function:

1. **Short-circuits immediately** if the tool count is already at or below `max_tools` -- no scoring overhead for small registries.
2. **Pins essential tools** that match the `ALWAYS_INCLUDE` set into a guaranteed slot, keeping remaining slots for scored tools.
3. **Scores and sorts** non-pinned tools by overlap count, then fills remaining slots from the top of that sorted list.

## The ALWAYS_INCLUDE Design Decision

The `ALWAYS_INCLUDE` frozenset -- hardcoded to `web_search`, `remember`, `recall`, `forget`, `create_pocket`, `add_widget`, and `remove_widget` -- reflects a deliberate product stance: these tools underpin PocketPaw's core value proposition (memory, search, and pocket management). Filtering them out based on message content would create unpredictable behavior. A user saying "what's the weather?" shouldn't lose access to memory tools just because the message contains no memory-related tokens.

## Why Token Overlap (Not Embeddings)

The approach is intentionally simple. Embedding-based semantic similarity would be more accurate but would require either an embedding model call (latency and cost) or a local embedding index (setup complexity). Token overlap runs in microseconds with no external dependencies, which matters because filtering happens on every agent invocation. The tradeoff is that it can miss synonyms -- a message about "files" won't score the `read_file` tool if the tool description uses "filesystem" -- but the practical impact is low because the `max_tools` default of 15 is generous.

## Integration Point

The module comment explicitly calls out the integration point: `filter_tools()` is intended to be called in `agents/loop.py` before tool definitions are passed to the router. This makes the filter a transparent middleware step -- the agent loop calls it, gets back a trimmed list, and proceeds normally.

## Edge Case Handling

If the user message is empty (tokenizes to an empty set), the function falls back to returning the first `max_tools` tools unchanged rather than attempting overlap scoring against an empty set. The `always_include` parameter can be overridden at call time, giving tests and specialized agent configurations the ability to pin different tool sets.

## Known Gaps

No `TODO`, `FIXME`, or `HACK` markers appear in the source. However, the algorithm has a known semantic gap: tools whose descriptions use synonyms or jargon not present in the user message will receive a zero score and may be filtered out even when relevant. A future improvement could use a small local vocabulary map (e.g., "file" -> "read_file") to boost scores for common synonyms without adding embedding dependencies.