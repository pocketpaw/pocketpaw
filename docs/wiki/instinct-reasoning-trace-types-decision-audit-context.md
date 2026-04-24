---
{
  "title": "Instinct Reasoning Trace Types — Decision Audit Context",
  "summary": "Defines the immutable data types that record the full reasoning context behind each proposed action: the queries made, soul memories consulted, KB articles injected, and tool calls executed. These types power the \"Why?\" audit drawer and enable humans to verify exactly what evidence the agent used.",
  "concepts": [
    "ReasoningTrace",
    "ToolCallRef",
    "FabricObjectSnapshot",
    "args_hash deduplication",
    "result_preview",
    "Why drawer",
    "audit context",
    "immutable snapshot",
    "fabric queries",
    "soul memories",
    "kb_articles injection"
  ],
  "categories": [
    "instinct engine",
    "audit and compliance",
    "data models",
    "enterprise edition"
  ],
  "source_docs": [
    "bf1a2ea45e29c5af"
  ],
  "backlinks": null,
  "word_count": 512,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

Audit logs that record *what* happened are useful. Audit logs that record *why* an AI agent made a decision are essential for trust. `trace.py` defines the schema for capturing the reasoning inputs — the evidence the agent had access to when it produced a proposal — so any stakeholder can later inspect the full decision context.

## ToolCallRef

Represents a single tool invocation captured during a proposal. Key fields:

- **tool** — the tool name, matching the event's `data["tool"]` key on the message bus.
- **args_hash** — a stable SHA-256 fingerprint of the serialized call arguments. This enables deduplication when the same tool is called with identical arguments multiple times during a single proposal (e.g., a fabric query issued twice for the same object ID). The `TraceCollector` uses this hash to avoid storing duplicate `ToolCallRef` entries.
- **result_preview** — the first 200 characters of the result string. Storing the full result would inflate the trace row unpredictably; 200 characters gives a human enough context to recognize what the tool returned without re-executing it.
- **duration_ms** — wall-clock time for the call, useful for identifying slow tool calls that may have caused the agent to time out or use stale results.

## ReasoningTrace

The full context envelope attached to a proposed action. Reference lists hold IDs only:

- **fabric_queries** — list of Fabric object IDs that were queried.
- **soul_memories** — list of soul memory IDs that were recalled.
- **kb_articles** — list of knowledge base article IDs that were injected.
- **tool_calls** — list of `ToolCallRef` objects.
- **prompt_version**, **backend**, **model** — identify the exact agent configuration at proposal time, enabling debugging when a model or prompt update changes behavior.

Reference fields hold IDs, not hydrated content. The `?hydrate=1` query parameter on the audit endpoint resolves these IDs at read time, keeping the stored trace compact while still supporting rich display.

## FabricObjectSnapshot

An immutable point-in-time copy of a Fabric object's state at the moment a decision was made. This exists because Fabric objects mutate over time — if you only store `object_id`, a later audit query sees the current state, not the state that actually influenced the decision. The snapshot rows are write-once (no update path in the store) and referenced from audit entries.

Key fields: `id` (prefixed `"fsnap_"`), `audit_id` (the audit entry it belongs to), `object_id` (the live Fabric object), `object_type`, `snapshot_data` (the full serialized object state), and `created_at`.

## Design Rationale

These three types were extracted into their own file (`trace.py`) so the store, the router, and the `TraceCollector` can all import them without creating circular dependencies. The types are pure Pydantic models with no business logic — making them the stable leaf node of the dependency graph.

## Known Gaps

- `FabricObjectSnapshot.snapshot_data` is typed as `dict[str, Any]` with no schema validation against the live Fabric object schema, so a snapshot of an outdated schema silently stores without warning.
- `ReasoningTrace` has no `token_count` or `context_window_usage` field; there is no way to know from the trace alone whether the agent was context-constrained when it made the decision.