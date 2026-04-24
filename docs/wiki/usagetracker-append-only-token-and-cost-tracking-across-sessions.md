---
{
  "title": "UsageTracker: Append-Only Token and Cost Tracking Across Sessions",
  "summary": "UsageTracker records per-request LLM usage (input tokens, output tokens, cached tokens, cost) as append-only JSONL entries in `~/.pocketpaw/usage.jsonl`, providing aggregation helpers for the `/api/v1/metrics/usage` endpoint. Thread safety is achieved with a `threading.Lock` so background telemetry threads and async request handlers can safely share the tracker.",
  "concepts": [
    "UsageTracker",
    "UsageRecord",
    "UsageSummary",
    "JSONL",
    "append-only",
    "threading.Lock",
    "token tracking",
    "cost tracking",
    "get_usage_tracker",
    "metrics endpoint"
  ],
  "categories": [
    "telemetry",
    "observability"
  ],
  "source_docs": [
    "0ed5d022c0b02e8c"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Every agent turn that calls an LLM backend consumes tokens and incurs cost. Without persistent tracking, users have no visibility into what they've spent, and operators can't detect runaway usage. `UsageTracker` solves this with the simplest possible durable store: an append-only newline-delimited JSON file. There is no database dependency, no migration step, and no startup cost.

## JSONL as the Persistence Format

Each call to `record()` appends one JSON object followed by a newline to `~/.pocketpaw/usage.jsonl`. JSONL is ideal for this use case:

- **Append-only writes** are atomic at the OS level on POSIX for writes that fit in the kernel buffer.
- **Line-by-line reads** allow streaming aggregation without loading the entire file into memory.
- **Human-readable** — operators can inspect usage with `tail -f` or `jq` without a query tool.
- **Crash-safe** — a process crash after a partial write at most corrupts the last incomplete line, which the reader skips gracefully.

## Thread Safety

`UsageTracker` uses `threading.Lock` because the agent loop can call `record()` from async tasks while a background thread (e.g., the usage reporter) simultaneously reads `get_records()`. Without the lock, a concurrent `record()` and `_iter_all_records()` could interleave file operations and produce a garbled read. The lock is held only for the duration of the file operation, keeping contention minimal.

## `UsageRecord` and `UsageSummary` Dataclasses

`UsageRecord` captures the full per-request context: backend name, model ID, input/output/cached token counts, session ID, and computed cost in USD. `UsageSummary` aggregates these across a time window. Both are dataclasses, so `asdict()` produces JSON-serialisable dicts with no custom serialiser needed.

```python
tracker = get_usage_tracker()
tracker.record(
    backend="anthropic",
    model="claude-sonnet-4-6",
    input_tokens=1200,
    output_tokens=380,
    cached_input_tokens=900,
    session_id="sess_abc",
    total_cost_usd=0.0045,
)
summary = tracker.get_summary(since=datetime(2026, 4, 1, tzinfo=UTC))
```

## `get_summary` for Metrics Endpoint

`get_summary(since)` filters records by timestamp and aggregates totals. It is called by the `/api/v1/metrics/usage` route to power the dashboard's usage panel. The `since` parameter allows the frontend to request different time windows (today, this week, this month) without the tracker maintaining pre-computed indices.

## `get_usage_tracker` Singleton Factory

`get_usage_tracker()` lazily constructs and caches the tracker singleton using a module-level variable. This avoids passing the tracker through every layer of the call stack while still making it injectable in tests by resetting the module-level reference.

## Known Gaps

- `_iter_all_records()` loads the entire JSONL file into a list. On a long-running instance with millions of records this becomes a memory and latency problem. A streaming implementation with early exit would be more scalable.
- Cost computation (`total_cost_usd`) is passed in by the caller rather than computed by the tracker. If the caller uses wrong pricing constants, the stored cost is wrong with no way to recalculate from stored tokens alone.
- The JSONL file grows indefinitely; there is no rotation, compaction, or archive mechanism. The `clear()` method truncates the file, but only manually.
- No schema versioning: if the `UsageRecord` fields change, old records in the JSONL may fail deserialization silently.
