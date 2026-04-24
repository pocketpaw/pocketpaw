---
{
  "title": "Fabric Event Payload Types and Action Constants",
  "summary": "This module pins the three canonical event shapes that drive Fabric's journal projection — object created, updated, and archived — as stable action name constants and payload builder functions. It was created specifically to replace PR #938's scope-filtering approach in the legacy SQLite store, which had two unfixable structural bugs: a schema migration gap and a pagination leak.",
  "concepts": [
    "journal events",
    "action constants",
    "payload builder",
    "pagination leak",
    "schema migration",
    "append-only journal",
    "scope exclusion",
    "archive vs delete",
    "event sourcing",
    "FABRIC_ACTION_PREFIX"
  ],
  "categories": [
    "fabric",
    "event sourcing",
    "journal",
    "scope filtering",
    "architecture"
  ],
  "source_docs": [
    "929aea661777eb0b"
  ],
  "backlinks": null,
  "word_count": 432,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Origin: Fixing Two Structural Bugs

The file header is unusually detailed because the design decision it captures is load-bearing. PR #938 attempted to add scope filtering to the legacy SQLite `FabricStore`. It failed for two reasons:

1. **Schema migration bug**: Existing databases never received the `scope` column because the migration logic had a gap. New installs worked; upgraded installs silently used the old schema.
2. **Pagination leak**: The query returned post-filter `objects` but computed `total` pre-filter, so a caller could detect that hidden objects existed by spotting a mismatch between `total` and `len(objects)`. This is a well-known information leak in paginated filtered APIs.

Rewriting Fabric writes as journal events resolves both blockers structurally. The journal is append-only, so there are no schema migrations. The projection computes `total` from the filtered set, so a pre-filter count is never exposed.

## Action Name Constants

The three constants pin the journal action namespace:

- `ACTION_OBJECT_CREATED = "fabric.object.created"`
- `ACTION_OBJECT_UPDATED = "fabric.object.updated"`
- `ACTION_OBJECT_ARCHIVED = "fabric.object.archived"`

The `FABRIC_ACTION_PREFIX = "fabric.object."` allows the projection to do a cheap prefix filter when querying the journal rather than matching all three names individually. `ALL_FABRIC_ACTIONS` is a tuple for callers that need to enumerate them.

These constants must never be renamed without a migration event on every existing journal. The projection replays events by action name; renaming breaks replay for all historical data.

## Payload Builder Functions

The three payload builders are module-level functions rather than class methods so both the store and external migration tools can call them without dragging in class state.

Key design decisions in each builder:

- **`object_created_payload`**: Scope is deliberately excluded from the payload. The journal's `EventEntry.scope` column is the canonical source of truth. Duplicating scope inside the payload would create a consistency hazard if the two copies ever diverged.
- **`object_updated_payload`**: Properties are a partial dict — the projection merges on top of existing state. Full replacement semantics would require a load-and-diff on the caller side and introduce race windows.
- **`object_archived_payload`**: Archive is an event, not a delete. The projection hides archived objects from current-state queries, but audit queries can still walk the full history.

```python
def object_archived_payload(*, object_id: str, reason: str = "") -> dict[str, Any]:
    return {
        "object_id": object_id,
        "reason": reason,
    }
```

## Stability Contract

Payloads are plain JSON-serializable dicts. Pydantic models are intentionally not embedded into the journal to keep the stored representation stable across Fabric refactors — if the `FabricObject` model gains or loses a field, old events remain parseable.

## Known Gaps

No gaps noted. The module is intentionally narrow: action names and payload shapes only.