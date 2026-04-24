---
{
  "title": "Session History Compaction Tests: Tier 1 Window Pruning, Budget Enforcement, LLM Summarization, and Cache",
  "summary": "This test suite validates PocketPaw's two-tier session history compaction system, which prevents unbounded conversation history from exhausting LLM context windows. Tier 1 collapses old messages into a synthetic summary, Tier 2 uses an LLM call to produce a semantic summary with disk caching, and the budget enforcer drops the oldest messages when compacted history still exceeds the token limit.",
  "concepts": [
    "session compaction",
    "Tier 1",
    "Tier 2",
    "rolling window",
    "budget enforcement",
    "LLM summarization",
    "disk cache",
    "MemoryManager",
    "MemoryEntry",
    "token budget",
    "get_session_history"
  ],
  "categories": [
    "memory",
    "testing",
    "compaction",
    "session history",
    "LLM context",
    "test"
  ],
  "source_docs": [
    "f1ca5f63e5a40525"
  ],
  "backlinks": null,
  "word_count": 511,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Long-running PocketPaw sessions accumulate conversation history that can exceed LLM context window limits. Compaction solves this by summarizing old messages so the agent retains contextual awareness without hitting token limits. The system has two tiers:

- **Tier 1**: A fast, rule-based rolling window — messages older than `recent_window` are replaced with a simple role-count summary.
- **Tier 2**: An optional LLM-generated semantic summary, cached to disk and invalidated when session content changes.

A budget enforcer acts as a final hard cap, dropping the oldest messages if the compacted history still exceeds the configured token budget.

## Tier 1: Rolling Window Compaction

```python
class TestTier1Compaction:
    async def test_short_session_unchanged(self):
        # < recent_window messages returned verbatim

    async def test_older_messages_collapsed(self):
        # messages beyond recent_window replaced with summary entry

    async def test_long_messages_truncated_in_summary(self):
        # very long collapsed messages are truncated before summarizing

    async def test_recent_messages_verbatim(self):
        # last recent_window messages always included in full
```

The `test_short_session_unchanged` test is a regression guard — compaction must be a no-op for short sessions. The truncation test prevents the summary entry itself from becoming excessively long when individual collapsed messages are very verbose.

## Budget Enforcement

```python
class TestBudgetEnforcement:
    async def test_over_budget_drops_oldest(self):
        # oldest messages dropped until total <= budget

    async def test_single_message_truncated(self):
        # even a single message is truncated if it exceeds budget alone

    async def test_budget_preserves_newest(self):
        # the most recent messages are always kept
```

The `test_single_message_truncated` test covers an important edge case: if a single message is longer than the entire budget (e.g., a user pasted a huge document), the system must still produce a usable history rather than raising an error. The budget enforcer must handle this by truncating the oversized message itself.

## Tier 2: LLM Summary with Disk Cache

```python
class TestTier2LLMSummary:
    async def test_llm_summary_called(self, tmp_path):
        # When Tier 2 enabled and no cache, LLM is called

    async def test_cached_summary_reused(self, tmp_path):
        # Second call with same session content returns cached summary

    async def test_stale_cache_invalidated(self, tmp_path):
        # After session content changes, LLM is called again

    async def test_no_sessions_path_falls_back(self):
        # When sessions_path not configured, Tier 2 silently skipped
```

The Tier 2 cache prevents redundant LLM calls on every agent turn. The cache key is derived from session content — if the conversation hasn't changed since the last summary, the cached summary is used. The `test_no_sessions_path_falls_back` test handles deployments where persistent storage isn't configured, ensuring Tier 2 silently degrades to Tier 1 only rather than crashing.

## Backward Compatibility

```python
class TestBackwardCompat:
    async def test_get_session_history_unchanged(self):
        # get_session_history() public API still returns expected format
```

This test verifies that the compaction refactor didn't change the shape of data returned by `get_session_history()`, which is used by backends to inject conversation history into LLM calls. A breaking change here would cause all backends to receive malformed history.

## Known Gaps

No test covers the behavior when the LLM summarization call itself fails (network error, rate limit). The expected fallback — presumably using Tier 1 summary only — is not validated. The token budget calculation method (character count vs. BPE token estimation) is not specified in the test assertions.