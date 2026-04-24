---
{
  "title": "Instinct Trace Collector — Bus-Subscriber Context Manager for Reasoning Capture",
  "summary": "An async context manager that subscribes to the message bus for the duration of a single agent proposal, collecting fabric queries, soul recalls, KB injections, and tool calls into a structured `ReasoningTrace`. Each proposal gets its own isolated collector instance, preventing cross-contamination between concurrent proposals.",
  "concepts": [
    "TraceCollector",
    "async context manager",
    "message bus subscription",
    "ReasoningTrace",
    "deduplication",
    "_hash_args",
    "_dedupe",
    "tool_start/tool_end events",
    "fabric_query event",
    "soul_recall event",
    "kb_inject event",
    "proposal isolation"
  ],
  "categories": [
    "instinct engine",
    "observability",
    "message bus",
    "enterprise edition"
  ],
  "source_docs": [
    "99f8470459c7cfed"
  ],
  "backlinks": null,
  "word_count": 482,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

The `TraceCollector` solves an instrumentation problem: how do you capture the reasoning inputs of an agent proposal without modifying the agent code? The answer is the message bus. PocketPaw's agent runtime emits structured events onto a system bus during execution. `TraceCollector` subscribes to that bus for the lifetime of one proposal and aggregates the relevant events into a `ReasoningTrace`.

## Usage Pattern

```python
async with TraceCollector(bus, prompt_version="v2", backend="pocketpaw", model="claude-sonnet-4-6") as collector:
    action = await agent.propose(pocket_id, context)
# collector.trace is now a ReasoningTrace with all captured events
await store.propose(pocket_id, ..., reasoning_trace=collector.trace)
```

The `async with` pattern ensures the bus subscription is always cleaned up — even if the proposal raises an exception — preventing leaked subscriptions from accumulating over time.

## Event Mapping

The collector handles five event types:

| Event type | Collected field | What it captures |
|---|---|---|
| `fabric_query` | `fabric_queries` | `data["object_id"]` |
| `soul_recall` | `soul_memories` | `data["memory_id"]` |
| `kb_inject` | `kb_articles` | `data["article_id"]` |
| `tool_start` | (stored temporarily by name) | Marks start time |
| `tool_end` | `tool_calls` | Builds `ToolCallRef` with args_hash and duration |

Unknown event types are silently ignored. This is intentional — the bus carries many event types beyond reasoning context, and unknown events should not cause trace failures.

## Deduplication

`_dedupe(values)` preserves insertion order while removing duplicate IDs. If the agent queries the same Fabric object twice during one proposal, the `fabric_queries` list contains it once. This keeps traces compact and meaningful — repetition indicates a bug in the agent's reasoning, not additional evidence.

`_hash_args(args)` produces a SHA-256 fingerprint of the JSON-serialized tool arguments. The hash is stored on `ToolCallRef` rather than the raw arguments for two reasons: it keeps the stored row small, and it enables exact-match deduplication of repeated tool calls without string comparison.

## Isolation — No Global State

Each `TraceCollector` instance holds its own `_partial_tools` dict (keyed by tool name, storing the start time) and its own `ReasoningTrace` under construction. Concurrent proposals each get their own collector instance, so their traces never commingle. This is the key design requirement — a global singleton would produce nonsensical mixed traces.

## prompt_version / backend / model

These three fields are set at construction time and recorded on the `ReasoningTrace`. They identify the exact agent configuration that produced the proposal. When a model update causes regressions in proposal quality, these fields let you filter the audit log to "proposals made with model X" and compare outcomes.

## Known Gaps

- `tool_start` and `tool_end` are matched by tool name only — if the same tool is called concurrently within one proposal (parallel tool calls), the `_partial_tools` dict will misattribute durations.
- There is no timeout for partial tool entries; if `tool_start` fires but `tool_end` never fires (tool crash), the partial entry accumulates in `_partial_tools` for the life of the collector without being recorded in the trace.