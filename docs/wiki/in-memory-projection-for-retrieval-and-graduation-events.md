---
{
  "title": "In-Memory Projection for Retrieval and Graduation Events",
  "summary": "Provides the read-side of the retrieval domain by replaying the org journal into two in-memory views: a recent-retrievals list (filterable by scope, actor, and pocket) and a per-memory graduation state table. A single replay pass builds both views because they share the same event stream.",
  "concepts": [
    "event replay",
    "in-memory projection",
    "RetrievalView",
    "GraduationStateRow",
    "bisect insertion",
    "rebuild from journal",
    "incremental apply",
    "scope visibility filter",
    "dataclasses vs Pydantic",
    "bootstrap sequence",
    "read model",
    "since_seq checkpoint"
  ],
  "categories": [
    "event-sourcing",
    "retrieval",
    "projection-pattern",
    "memory-graduation"
  ],
  "source_docs": [
    "ee/retrieval/projection.py"
  ],
  "backlinks": null,
  "word_count": 477,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/retrieval/projection.py` is the read model for PocketPaw's retrieval domain. It consumes events from the org journal and folds them into queryable in-memory structures, replacing a JSONL file that PR #936 and #937 used as a shared data store.

## Two Views, One Projection

Two logical read surfaces live on a single `RetrievalProjection` instance:

**RetrievalView** — represents one projected retrieval after a `retrieval.query` event is replayed. Fields include `request_id`, `query`, `actor_id`, `scope`, `correlation_id`, `strategy`, `sources_queried`, `sources_failed`, `candidates`, `picked`, `latency_ms`, and `ts`. The list is kept in insertion order and bounded at a configurable maximum so memory does not grow without bound. The `_RetrievalRow.__lt__` method enables binary-search insertion via `bisect.insort`, keeping the list sorted by timestamp without a full re-sort on every event.

**GraduationStateRow** — holds the most-recent graduation verdict for one `memory_id`. Every `graduation.applied` event overwrites the previous row for that memory. This means the projection always reflects the current tier without accumulating one row per decision.

Keeping both views in one projection means one journal replay pass populates both. Soul-protocol's `replay_from` iterates the full stream; filtering to `ALL_RETRIEVAL_ACTIONS` discards irrelevant events early. The alternative — two separate projections each replaying the same stream — would double replay time for no added correctness.

## Rebuild and Incremental Apply

```python
# Rebuild from scratch
projection.rebuild(journal, since_seq=0)

# Incremental update after a write
projection.apply(entry)
```

The `rebuild` method replays from a checkpoint sequence number (`since_seq`) rather than always from 0. On startup, `RetrievalJournalStore.bootstrap` calls `rebuild` once and caches the projection. Subsequent writes call `apply` with the newly emitted `EventEntry` so reads are consistent immediately — no round-trip back to the journal.

## Filter Visibility Guard

All retrieval queries pass through `ee.fabric.policy.filter_visible` before results are returned to callers. This guard enforces scope-based visibility rules: an actor can only see retrievals that fall within their permitted scopes. Without this guard, a per-pocket actor could read retrievals from other pockets simply by querying the shared projection.

## Query Methods

`RetrievalProjection` exposes:

- `recent(limit, scope_filter, actor_filter, pocket_filter, correlation_id)` — returns the N most recent retrievals matching the filters
- `graduation_state(memory_id)` — returns the most recent graduation row for one memory
- `all_graduation_states()` — returns all rows, used by the policy scan

## Why Dataclasses, Not Pydantic

The row types (`RetrievalView`, `GraduationStateRow`, `_RetrievalRow`) are plain Python dataclasses. Pydantic's validation machinery runs on every instantiation, which adds overhead on the hot path where thousands of events are replayed. The router layer converts projection rows to Pydantic response models at the HTTP boundary, where the cost is paid once per request.

## Known Gaps

The `since_seq` bootstrap pattern assumes the projection is rebuilt in-process on startup. There is no cross-process cache; if two server workers run simultaneously, each holds an independent projection rebuilt from the same journal — reads are consistent within a worker but the two workers may diverge transiently during a bootstrap window.