---
{
  "title": "Journal-Backed Write Store for Widget Events",
  "summary": "Provides the write facade for the widget domain, emitting all five widget action types onto the org journal and folding each emission into the shared in-memory projection for immediate read consistency. Adds a seq-returning variant of the interaction writer to support the POST /widgets/track endpoint's cursor ack requirement.",
  "concepts": [
    "widget store",
    "write facade",
    "JSONL migration",
    "SQLiteJournalBackend",
    "BEGIN IMMEDIATE",
    "log_widget_interaction_with_seq",
    "sequence number ack",
    "system actor IDs",
    "projection consistency",
    "asyncio.Lock limitation",
    "emit methods"
  ],
  "categories": [
    "widget-system",
    "event-sourcing",
    "storage",
    "journal-pattern"
  ],
  "source_docs": [
    "ee/widget/store.py"
  ],
  "backlinks": null,
  "word_count": 438,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/widget/store.py` is the write path for the widget journal projection domain. It mirrors the thin-facade pattern from `ee/retrieval/store.py`: emit an event, fold it into the projection, return. No policy logic, no HTTP concerns.

## Why the JSONL Approach Was Replaced

PR #941 wrote widget interactions to `~/.pocketpaw/widget-interactions.jsonl` behind an `asyncio.Lock`. The same two problems applied here as in the retrieval domain:

1. **asyncio.Lock is per-process.** Multiple PocketPaw processes would race on the file.
2. **JSONL had no atomic multi-write.** PR #941's graduation apply path and PR #942's co-occurrence detection both read from the same file, with no transactional boundary between the read and the decision write.

SQLite WAL transactions eliminate both problems.

## The `log_widget_interaction_with_seq` Variant

Standard journal writes via `Journal.append` discard the assigned sequence number — the return value is `EventEntry` but not the seq. The `POST /widgets/track` endpoint needs the seq on its ack response so UI clients can optionally use it as a journal cursor for consistency polling.

To solve this without losing the transaction boundary, `log_widget_interaction_with_seq` goes through the journal's underlying `SQLiteJournalBackend.append`, which returns `(EventEntry, seq)` atomically:

```python
async def log_widget_interaction_with_seq(
    self, *, widget_name, scope, actor, surface, ...
) -> tuple[EventEntry, int]:
    payload = widget_interaction_payload(...)
    entry, seq = await self._journal.backend.append(payload, ...)
    self._projection.apply(entry)
    return entry, seq
```

Going through the backend directly keeps the INSERT and seq read inside the same `BEGIN IMMEDIATE` transaction — using `Journal.last_entry` after `Journal.append` would introduce a race window where another process could insert between the two calls.

## Emit Methods

| Method | Event Emitted |
|---|---|
| `log_widget_interaction` | `widget.interaction.recorded` |
| `log_widget_interaction_with_seq` | same, returns `(entry, seq)` |
| `log_widget_graduation` | `widget.graduated` |
| `log_cooccurrence` | `widget.cooccurrence.detected` |
| `log_cooccurrence_accepted` | `widget.cooccurrence.accepted` |
| `log_cooccurrence_dismissed` | `widget.cooccurrence.dismissed` |

## System Actor IDs

```python
_SYSTEM_WIDGET_ACTOR_ID       = "system:widget"
_SYSTEM_GRADUATION_ACTOR_ID   = "system:graduation"
_SYSTEM_COOCCURRENCE_ACTOR_ID = "system:cooccurrence"
```

Three system actors rather than one, mirroring the three decision concerns. This allows the projection's actor-based filtering to distinguish between user-triggered interactions (`actor_id` from the request), system-triggered graduations, and system-triggered co-occurrence emissions.

## Projection Consistency

Every emit immediately calls `self._projection.apply(entry)`. The projection is the live read path for all GET endpoints; if a write isn't folded in synchronously, a GET made in the same async cycle (before another request triggers a journal query) would return stale data.

## Known Gaps

The `log_cooccurrence_accepted` and `log_cooccurrence_dismissed` methods were added in Cluster B Sub-PR #2. The `bootstrap` method replays all events including these newer action types, but older journal databases that pre-date the Sub-PR #2 schema will simply have no accepted/dismissed events to replay — the projection's accept/dismiss sets will start empty, which is correct.