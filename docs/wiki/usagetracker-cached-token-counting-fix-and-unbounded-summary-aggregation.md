---
{
  "title": "UsageTracker: Cached Token Counting Fix and Unbounded Summary Aggregation",
  "summary": "These tests document and verify two bug fixes in `UsageTracker`: cached input tokens were silently excluded from `total_tokens`, and `get_summary()` was capped at 10,000 records, causing understated lifetime aggregation totals for high-volume installations. Both fixes ensure accurate cost accounting across all Anthropic and OpenAI backends.",
  "concepts": [
    "UsageTracker",
    "cached_input_tokens",
    "total_tokens",
    "get_summary",
    "record_limit",
    "cost_estimation",
    "JSONL",
    "Anthropic",
    "OpenAI",
    "token_counting"
  ],
  "categories": [
    "usage-tracking",
    "testing",
    "cost-management",
    "test"
  ],
  "source_docs": [
    "3a85162ca667912d"
  ],
  "backlinks": null,
  "word_count": 394,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`UsageTracker` records per-call token counts to a JSONL file and aggregates them into summaries for cost reporting. Two independently discovered bugs caused silent undercounting, with the test file serving as the authoritative specification for both correct behaviors.

## Bug 1: Cached Input Tokens Excluded from `total_tokens`

Anthropics's API returns `input_tokens`, `output_tokens`, and `cached_input_tokens` separately. The original `record()` implementation computed:

```
total_tokens = input_tokens + output_tokens
```

This silently dropped `cached_input_tokens` from the count. While cached tokens are cheaper, they are still real tokens processed by the model and must be included for accurate capacity and cost reporting.

The fix changes the formula to:

```
total_tokens = input_tokens + output_tokens + cached_input_tokens
```

Tests verify this for a single record, for persistence (the JSONL line must contain the correct `total_tokens`), and for summary aggregation across multiple records:

```python
def test_total_tokens_with_cached(self, tmp_path):
    rec = tracker.record(
        backend="anthropic", model="claude-3-5-sonnet-20241022",
        input_tokens=100, output_tokens=50, cached_input_tokens=200,
    )
    assert rec.total_tokens == 350  # 100 + 50 + 200
```

## Bug 2: `get_summary()` Capped at 10,000 Records

The original `get_summary()` called `get_records(limit=10_000)` internally, meaning any installation that had recorded more than 10,000 lifetime API calls would produce a summary that silently undercounted total tokens and costs.

The fix removes the limit from the internal summary query so all records are read:

```python
def test_summary_counts_all_records_beyond_default_limit(self, tmp_path):
    # Write 10,001 records, verify summary covers all of them

def test_summary_counts_all_records_beyond_old_hardcoded_limit(self, tmp_path):
    # Write 15,000 records, confirm exact total
```

Importantly, `get_records()` still respects its `limit` parameter when called directly — the fix only removes the cap from the internal summary path.

## `since` Filter with All Records

`get_summary(since=timestamp)` filters records by timestamp. A separate test confirms this filter works correctly when the summary reads all records (not just the first 10,000), preventing a regression where the `since` filter could interact incorrectly with the new unbounded read.

## Cost Estimation

`_estimate_cost` maps model names to per-token pricing. Tests verify:
- Known models (`claude-3-5-sonnet-20241022`, `gpt-4o`) return a cost value
- Prefix matching handles model version variants
- Unknown models return `None` rather than raising
- Cached input tokens are billed at a lower rate than regular input tokens (reflecting Anthropic's cache-read pricing discount)

## Known Gaps

No TODOs. The `_write_n_records` helper used in `TestSummaryCoversAllRecords` writes minimal records to avoid test runtime; a future improvement could test with varied token counts to verify aggregation arithmetic more rigorously.