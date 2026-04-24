---
{
  "title": "Decision Trace Collector: Lifecycle, Event Aggregation, Deduplication, and Snapshot Persistence Tests",
  "summary": "This suite tests the `TraceCollector` context manager that records which Fabric objects, soul memories, KB articles, and tool calls an agent consulted while generating a decision. It also verifies the `FabricObjectSnapshot` persistence layer that captures point-in-time object state alongside each audit entry.",
  "concepts": [
    "ReasoningTrace",
    "TraceCollector",
    "MessageBus",
    "FabricObjectSnapshot",
    "event aggregation",
    "deduplication",
    "tool call pairing",
    "decision audit",
    "explainability",
    "async context manager"
  ],
  "categories": [
    "instinct",
    "audit",
    "testing",
    "tracing",
    "test"
  ],
  "source_docs": [
    "4fdb019bfac956de"
  ],
  "backlinks": null,
  "word_count": 607,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Decision Traces Exist

When an agent proposes an action, knowing *what information it had access to* is as important as knowing what it decided. Without traces, an audit entry only shows the outcome; with traces, reviewers can replay the agent's context — which customer record it read, which memory it recalled, which KB article informed the recommendation. This is especially critical for regulated workflows where explainability is required.

## ReasoningTrace Model

The `ReasoningTrace` Pydantic model has four reference lists (`fabric_queries`, `soul_memories`, `kb_articles`, `tool_calls`) plus metadata fields (`prompt_version`, `backend`, `model`, `token_counts`).

`test_defaults_produce_empty_collections` verifies that an uninitialized trace has empty lists rather than `None`, preventing downstream `len()` and iteration from raising `TypeError`.

`test_round_trip_serialization` validates full Pydantic serialize→deserialize fidelity, including the nested `ToolCallRef` model. This matters because traces are stored as JSON blobs in SQLite and must survive the round trip intact.

## TraceCollector Lifecycle

The collector is an async context manager that subscribes to the `MessageBus` on `__aenter__` and unsubscribes on `__aexit__`.

```python
async with TraceCollector(bus) as collector:
    # bus.subscribers has exactly one entry here
    pass
# bus.subscribers is empty again
```

**`test_unsubscribes_even_when_body_raises`** is the most important lifecycle test: if the agent code inside the `async with` block raises, the collector must still unsubscribe. Without this, a crashed agent turn would leave a dangling subscriber that accumulates events from future turns, corrupting subsequent traces.

A `FakeBus` replaces the real `MessageBus` to avoid pulling in the full event infrastructure. It implements only `subscribe_system`, `unsubscribe_system`, and `publish` — the three methods the collector uses.

## Event Aggregation

The collector listens for typed events and routes them into the appropriate trace list:

| Event type | Trace field | Key extracted |
|---|---|---|
| `fabric_query` | `fabric_queries` | `object_id` |
| `soul_recall` | `soul_memories` | `memory_id` |
| `kb_inject` | `kb_articles` | `article_id` |
| `tool_start` + `tool_end`/`tool_result` | `tool_calls` | tool name + args hash |

**Tool call pairing** — `tool_start` opens a timer; `tool_end` or `tool_result` (aliased) closes it and records `duration_ms`. The alias test (`test_tool_result_alias_event_also_captured`) prevents the system from missing tool calls from backends that emit `tool_result` instead of `tool_end`.

**Long result truncation** — results longer than 200 characters are truncated with `...`. This prevents traces from becoming storage liabilities when a tool returns multi-kilobyte payloads. The test verifies exactly `len(preview) == 200` with a trailing ellipsis.

**Deduplication on exit** — the same `object_id` published twice produces only one entry in `fabric_queries`. This prevents inflated reference counts when an agent calls the same object repeatedly during reasoning. `test_reference_lists_are_deduplicated_on_exit` verifies that deduplication happens at `__aexit__` time (not during accumulation), so duplicate suppression is a cleanup step, not an in-flight filter.

**Tool call merging** — tool calls with the same tool name *and* args hash are merged into a single `ToolCallRef`. Different args produce separate entries. This balances deduplication (same query repeated) against completeness (different queries to same tool).

**Resilience** — unknown event types and malformed event data (missing keys, wrong types) are silently skipped. This prevents a misbehaving publisher from crashing the collector and losing the entire trace.

## FabricObjectSnapshot Persistence

Snapshots capture the state of a Fabric object at the moment of a decision. Three store-level tests:

- **`test_record_and_read_snapshot`** — verifies that a snapshot's `object_type`, `object_id`, and `snapshot` dict survive the SQLite round trip.
- **`test_snapshots_for_audit_orders_oldest_first`** — audit view shows snapshots in insertion order, matching the sequence the agent consulted them.
- **`test_snapshots_for_object_orders_newest_first`** — object history view shows newest snapshot first, matching a "what changed most recently" use case.

## Known Gaps

None flagged. The suite was created as part of "Move 2 PR-A" and notes it locks the context-manager lifecycle, aggregation, deduplication, and SQLite persistence paths.