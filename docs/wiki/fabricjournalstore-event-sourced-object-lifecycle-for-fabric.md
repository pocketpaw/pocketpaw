---
{
  "title": "FabricJournalStore — Event-Sourced Object Lifecycle for Fabric",
  "summary": "`FabricJournalStore` is the Wave 3 replacement for the scope-filtering slice of PR #938, providing event-sourced create, update, archive, and query operations over Fabric objects backed by the org journal and served through an in-memory projection. Every write appends an `EventEntry` to the journal and immediately folds it into the live projection, so reads after a write are always consistent without a rebuild.",
  "concepts": [
    "FabricJournalStore",
    "event sourcing",
    "journal append",
    "FabricProjection",
    "bootstrap",
    "scope invariant",
    "read-after-write",
    "scope opacity",
    "not-found vs forbidden",
    "soul_protocol Journal"
  ],
  "categories": [
    "fabric",
    "event sourcing",
    "journal",
    "scope filtering",
    "CQRS"
  ],
  "source_docs": [
    "2d6e570bbe8dbd14"
  ],
  "backlinks": null,
  "word_count": 451,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Architecture

The store is a thin coordination layer over two components:

- **`Journal`** (from `soul_protocol.engine`) — the append-only event log. Every write call appends one `EventEntry`.
- **`FabricProjection`** — an in-memory state dictionary rebuilt from journal replay. Reads are served here after scope filtering.

Write → journal append → projection fold → read from projection. This guarantees read-after-write consistency within a single process without relying on the journal being readable immediately after a write.

## Bootstrap

`bootstrap(since_seq=0)` replays journal events into the projection at process start. The `since_seq` parameter lets operators who persist a cursor skip already-applied events and reduce startup time. Without `bootstrap()`, the projection is empty and all reads return nothing.

## Scope Invariant

Every write method requires a non-empty `scope` list. The `_require_scope` guard fires before the `EventEntry` is constructed so the error message names the Fabric API rather than producing a cryptic Pydantic validation error deep inside soul-protocol. This is a usability guard: callers that forget scope get a clear message at the right stack frame.

```python
def _require_scope(scope: list[str]) -> None:
    if not scope:
        raise ValueError(
            "FabricJournalStore requires a non-empty scope on every write — "
            "the journal invariant refuses events with scope=[]."
        )
```

## Read-After-Write Pattern

After `create()` appends an event and folds it into the projection, it queries the projection by type to return the canonical projected object (not the raw input). This is defensive: the projection may have normalised or enriched the object during apply, and returning the projected form ensures the caller sees exactly what future reads will return.

## Scope Opacity in `get()`

`get(object_id, requester_scopes=...)` returns `None` both when the object does not exist and when it exists but the caller's scope does not grant access. This is intentional — an unauthorized caller cannot use the response to probe whether a hidden record exists. The indistinguishability between "not found" and "forbidden" prevents scope configurations from being used as an oracle for hidden data.

## Narrow Scope

The store is deliberately narrow — object lifecycle only (create, update, archive, query). Object-type definitions and links remain in the legacy `FabricStore`. This keeps the journal path focused on the data that benefits from scope filtering (per-tenant objects) while leaving low-churn schema data (types, links) in the simpler SQLite path.

## Known Gaps

- **`update()` is not fully implemented** in the source snippet shown — `FabricObject | None` is the return type signature but the update path was not yet visible in the extracted structure. Callers should verify update semantics before using in production.
- The `projection` property is explicitly marked "not part of the stable API" — reaching for it in production code is a signal to add a dedicated method instead.