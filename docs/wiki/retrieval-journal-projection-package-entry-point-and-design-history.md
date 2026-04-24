---
{
  "title": "Retrieval Journal Projection — Package Entry Point and Design History",
  "summary": "The `ee.retrieval` package implements an observable retrieval trail and access-count graduation policy as a projection over the org journal, superseding two earlier JSONL-based designs. This module re-exports every public symbol and documents the architectural decision to consolidate onto the journal rather than maintain a parallel JSONL sink.",
  "concepts": [
    "RetrievalProjection",
    "RetrievalJournalStore",
    "graduation policy",
    "GraduationDecision",
    "GraduationKind",
    "journal projection pattern",
    "event sourcing",
    "retrieval.query event",
    "graduation.applied event",
    "access-count graduation",
    "soul memory tiers",
    "JSONL sink retirement"
  ],
  "categories": [
    "retrieval",
    "soul protocol integration",
    "enterprise edition",
    "memory graduation"
  ],
  "source_docs": [
    "3f0523497ec3a4ca"
  ],
  "backlinks": null,
  "word_count": 430,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Design History and Why This Exists

The `__init__.py` comment is unusually detailed because the module replaced two previously held PRs (`#936` and `#937`) that solved the same problem with a different approach. Understanding the transition is essential for understanding the current architecture.

**The old approach (PRs #936, #937):** A side-channel JSONL file at `~/.pocketpaw/retrieval.jsonl` with its own mutex. Retrieval queries were appended to this file, and a separate graduation policy module read it to count accesses. This worked but created a third data silo (alongside the soul database and the org journal) and required a mutex to serialize writes.

**The new approach:** Retrieval queries emit journal events (`retrieval.query`) into the org journal, and a `RetrievalProjection` reads them back as a filtered view. Graduation events (`graduation.applied`) are also journal events. The JSONL sink and its mutex are retired entirely.

This is an instance of the **event-sourced projection pattern**: the journal is the write-ahead log, and the projection is a read model built from it. The benefit is that the org journal becomes the single source of truth for all org-level events, including retrieval activity.

## Exported Symbols by Sub-module

### ee.retrieval.events
Action name constants (`ACTION_RETRIEVAL_QUERY`, `ACTION_GRADUATION_APPLIED`, `ALL_RETRIEVAL_ACTIONS`) and payload builder functions (`retrieval_query_payload`, `graduation_applied_payload`). These are the "vocabulary" for emitting retrieval events — callers that want to log retrieval queries out-of-band use these builders to produce consistently shaped payloads.

### ee.retrieval.store — Write Path
`RetrievalJournalStore` — the write-side adapter that emits `retrieval.query` events into the org journal. Thin wrapper around `Journal.append()`.

### ee.retrieval.projection — Read Path
`RetrievalProjection` — reads `retrieval.query` events from the journal and exposes them as `RetrievalView` objects. `GraduationStateRow` represents the accumulated access state per memory ID used by the graduation policy.

### ee.retrieval.policy — Graduation Decisions
`GraduationDecision`, `GraduationKind`, `GraduationReport` — the decision types. `scan_for_graduations(projection, ...)` applies the policy: memories accessed more than `DEFAULT_EPISODIC_THRESHOLD` (or `DEFAULT_SEMANTIC_THRESHOLD`) times in `DEFAULT_WINDOW_DAYS` days are candidates for promotion to a higher-permanence storage tier. `apply_decisions(decisions, soul)` executes the promotions.

## Graduation Policy Context

In soul-protocol's 5-tier memory architecture, memories that are accessed frequently are candidates for "graduation" — promotion from a shorter-lived tier (e.g., episodic buffer) to a longer-lived tier (e.g., semantic core). The retrieval access count is the signal: if a memory is recalled repeatedly, it has demonstrated relevance and should be preserved more permanently. The journal projection provides an accurate access count without requiring a separate counter database.

## Known Gaps

- `DEFAULT_WINDOW_DAYS`, `DEFAULT_EPISODIC_THRESHOLD`, and `DEFAULT_SEMANTIC_THRESHOLD` are module-level constants with no per-org or per-pocket configuration override path.
- `apply_decisions()` applies graduations synchronously; for large graduation batches this could block the request path.