---
{
  "title": "Journal-Backed Write Store for Retrieval and Graduation Events",
  "summary": "Provides the write path for the retrieval domain by appending `retrieval.query` and `graduation.applied` events to the org journal and immediately folding them into the shared in-memory projection. Replaces the JSONL-plus-asyncio-lock pattern from PR #936 with SQLite WAL semantics that are multi-process safe by design.",
  "concepts": [
    "journal store",
    "write path",
    "asyncio.Lock limitation",
    "multi-process safety",
    "SQLite WAL",
    "projection consistency",
    "bootstrap rebuild",
    "system actor IDs",
    "JSONL migration",
    "retrieval.query emission",
    "graduation.applied emission"
  ],
  "categories": [
    "event-sourcing",
    "retrieval",
    "storage",
    "journal-pattern"
  ],
  "source_docs": [
    "ee/retrieval/store.py"
  ],
  "backlinks": null,
  "word_count": 445,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/retrieval/store.py` is the write facade for PocketPaw's retrieval domain. It handles two emission concerns — logging that a retrieval happened and recording that a graduation decision was applied — and keeps them off the file system by routing both through the org journal.

## Why the JSONL Approach Failed

The previous design (from PR #936) wrote retrieval traces to `~/.pocketpaw/retrieval.jsonl` behind an `asyncio.Lock`. Two problems motivated the replacement:

1. **The lock didn't protect against multiple processes.** An `asyncio.Lock` is per-event-loop. If two PocketPaw workers ran simultaneously — for example, a background graduation scheduler and the main API server — they both held separate lock objects and could corrupt the JSONL file with interleaved writes.
2. **Graduation state also lived in JSONL.** PR #937 appended graduation decisions to the same file. Any reader needed to scan the full file to reconstruct state, and the scan wasn't idempotent if the file was partially written.

The journal backend uses `BEGIN IMMEDIATE` transactions, so SQLite's WAL handles write serialization across all processes accessing the same database file.

## API Surface

```python
store = RetrievalJournalStore(journal, projection=projection)
store.bootstrap(since_seq=0)  # Warm the projection on startup

# Log a retrieval
await store.log_retrieval(
    scope=["org:acme"],
    query="what did the user say yesterday",
    request_id=uuid4(),
    strategy="bm25",
    sources_queried=["episodic", "semantic"],
    sources_failed=[],
    candidates=[(mem_id, 0.91, "semantic")],
    picked=[mem_id],
    latency_ms=42,
    pocket_id="pocket:default",
)

# Apply a graduation decision
await store.log_graduation(decision)
```

## Projection Consistency

Every emit call appends the event to the journal **and** immediately calls `projection.apply(entry)` with the returned `EventEntry`. This keeps the in-memory projection consistent with the journal without waiting for a full rebuild. The pattern is the same as `ee/widget/store.py` — the projection is the live read path; if a write isn't folded in synchronously, a read made in the same request cycle would return stale data.

## Bootstrap

`bootstrap(since_seq)` calls `RetrievalProjection.rebuild(journal, since_seq)` and returns the number of events replayed. The router calls `bootstrap(since_seq=0)` on first use to warm the projection from the full journal. Subsequent writes go through `log_retrieval` / `log_graduation` and fold incrementally.

## Actor Defaults

Two module-level constants define system actor IDs:

- `_SYSTEM_RETRIEVAL_ACTOR_ID = "system:retrieval"`
- `_SYSTEM_GRADUATION_ACTOR_ID = "system:graduation"`

Callers can override these per-call. The defaults are used when a retrieval is logged by an internal path rather than a named user-facing actor. Having stable system actor IDs makes it easy to filter out internal noise when querying the projection.

## Known Gaps

The store has no explicit sequence-number return on `log_retrieval`. If a caller needs to pin a journal cursor after a retrieval write (for example, to poll for the graduation decision that follows), it must call `Journal.last_seq` separately. Contrast with `WidgetJournalStore.log_widget_interaction_with_seq`, which was added explicitly to close this gap for the widget track endpoint.