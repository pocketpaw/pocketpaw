---
{
  "title": "Instinct Decision Pipeline — Package Entry Point",
  "summary": "The `ee.instinct` package is PocketPaw's human-in-the-loop decision engine: agents propose actions, humans approve or edit them, approved actions execute, and any edits are captured as corrections that feed back into the soul for future improvement. This module re-exports every public symbol from the five sub-modules so consumers import from a single namespace.",
  "concepts": [
    "decision pipeline",
    "human-in-the-loop",
    "action proposal",
    "correction loop",
    "soul learning",
    "InstinctStore",
    "ReasoningTrace",
    "TraceCollector",
    "audit log",
    "Paw OS",
    "package facade"
  ],
  "categories": [
    "instinct engine",
    "decision pipeline",
    "enterprise edition",
    "agent autonomy"
  ],
  "source_docs": [
    "e0465e8cf707208f"
  ],
  "backlinks": null,
  "word_count": 363,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Instinct is the decision pipeline for Paw OS. It answers the question of how an AI agent and a human owner share control over consequential actions. The full loop is: agent proposes → human approves (optionally edits) → action executes → correction captured → soul learns → next proposal improves.

The `__init__.py` serves as a flat re-export facade. Consumers import `from ee.instinct import Action, InstinctStore` rather than drilling into sub-modules, which decouples the internal file layout from external callers.

## Exported Symbols by Sub-module

### ee.instinct.models
`Action`, `ActionCategory`, `ActionContext`, `ActionPriority`, `ActionStatus`, `ActionTrigger`, `AuditCategory`, `AuditEntry` — the core Pydantic data model for every proposed action and its lifecycle state.

### ee.instinct.store
`InstinctStore` — the async SQLite persistence layer. Handles proposals, approvals, rejections, audit logging, correction recording, and fabric snapshots in one place.

### ee.instinct.correction
`Correction`, `CorrectionPatch`, `compute_patches`, `summarize_correction` — the diff layer that captures what a human changed before approving an action, turning edits into structured learning signals.

### ee.instinct.trace
`ReasoningTrace`, `ToolCallRef`, `FabricObjectSnapshot` — the audit context attached to each proposal, recording which queries, soul memories, and tool calls informed the decision.

### ee.instinct.trace_collector
`TraceCollector` — the async context manager that subscribes to the message bus during a proposal and assembles a `ReasoningTrace` automatically.

## Lifecycle Summary

```python
# Agent proposes
async with TraceCollector(bus) as trace:
    action = await agent.propose(...)

# Store with trace
await store.propose(pocket_id, ..., reasoning_trace=trace)

# Human edits and approves via router
# router diffs stored vs approved, records Correction
patches = compute_patches(before=action, after=approved)
correction = Correction(action_id=action.id, patches=patches, ...)
await store.record_correction(correction)

# Soul bridge promotes repeated edits to procedural memory
await soul_bridge.record(correction, action)
```

## Design Rationale

The separation of concerns across five files is intentional. `models.py` is the stable contract — it changes only when the domain evolves. `store.py` owns all I/O. `correction.py` owns the diff semantics. `trace.py` owns the audit context schema. `trace_collector.py` owns the runtime bus subscription. This makes each layer independently testable and replaceable.

## Known Gaps

- No async background processing of approved actions; execution is a caller responsibility not wired at the Instinct layer.
- The soul learning loop only fires on corrections captured through the router; direct `store.record_correction()` calls bypass the `CorrectionSoulBridge`.