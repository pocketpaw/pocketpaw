---
{
  "title": "FabricProjection — In-Memory Journal Projection for Fabric Objects",
  "summary": "`FabricProjection` replays `fabric.object.*` journal events into an in-memory dictionary to maintain a current-state view of all Fabric objects, applying scope filters before computing query totals to permanently fix the pagination leak that blocked PR #938. It is a deliberately minimal CQRS read model: rebuild from the journal on startup, apply events incrementally, serve scoped queries.",
  "concepts": [
    "FabricProjection",
    "CQRS read model",
    "_ProjectedObject",
    "pagination leak fix",
    "rebuild",
    "apply",
    "scope filtering",
    "journal replay",
    "since_seq cursor",
    "as_public",
    "duck-typed scope"
  ],
  "categories": [
    "fabric",
    "CQRS",
    "projection",
    "event sourcing",
    "scope filtering"
  ],
  "source_docs": [
    "64ea1f49bcaeeb73"
  ],
  "backlinks": null,
  "word_count": 435,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why a Projection?

The projection exists to solve the pagination leak documented in PR #938. When scope filtering happened in a SQLite query, `total` was computed pre-filter and `objects` was returned post-filter. A caller who received `total=10, objects=[]` (all 10 hidden) could infer that hidden records exist.

Moving scope filtering into the projection means `total` is derived from the filtered list, never from a raw DB count. There is no pre-filter number anywhere in the read path.

## `_ProjectedObject` Internal Row

The projection maintains a dict of `_ProjectedObject` instances rather than `FabricObject` directly. The reason is that the journal `EventEntry` carries `scope` on the entry itself, not inside the payload. Keeping `scope` on the internal row avoids adding it as a model field on `FabricObject` (which would break the schema) while still making it available for policy decisions.

`as_public()` bridges the two: it `model_copy(deep=True)`s the `FabricObject` and attaches `scope` as a plain Python attribute via `object.__setattr__`. The policy engine duck-types its entity argument, reading `scope` off whatever attribute or key it finds.

## Rebuild and Apply

- **`rebuild(journal, since_seq=0)`** — replays all `fabric.object.*` events from the journal. The `since_seq` cursor allows warm restarts that skip already-applied events. Returns the count of applied events for diagnostics.
- **`apply(entry)`** — processes a single `EventEntry` incrementally. The store calls this immediately after every write so the projection is always in sync within a process, no periodic rebuild needed.

The three event handlers:

- `fabric.object.created` — builds a `FabricObject` from the payload, stores it with the entry's scope and sequence number.
- `fabric.object.updated` — merges partial properties onto the existing row. Unknown `object_id` is logged and silently ignored (journal may contain events for objects from a tenant slice not present in this projection instance).
- `fabric.object.archived` — marks the internal row `archived=True`. Archived objects are excluded from `query()` results but remain in the state dict for audit purposes.

## Query

`query(q, requester_scopes)` filters the state dict in three passes: type filter, property filter, then scope filter via `filter_visible`. Pagination (`limit`, `offset`) is applied last so `total` reflects the post-scope, post-filter count.

`requester_scopes=None` or `[]` bypasses scope filtering — this is the admin/system path used by `FabricJournalStore._lookup()` for internal read-after-write confirmation.

## Known Gaps

- The linked-to traversal (`FabricQuery.linked_to`) is not yet implemented in the projection. It is only supported in the legacy `FabricStore.query()` path. Callers using graph traversal queries must use the legacy store until this is added.
- Memory usage scales with the number of live (non-archived) Fabric objects. For installations with millions of objects, an LRU eviction strategy or a secondary indexed store would be needed.