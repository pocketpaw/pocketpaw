---
{
  "title": "Mission Control File Store: JSON-Backed Persistence with In-Memory Indexes",
  "summary": "FileMissionControlStore implements MissionControlStoreProtocol using one JSON file per entity type, combined with in-memory Python dicts for O(1) lookups. Atomic writes via temp-file rename prevent data corruption, and a singleton factory ensures all subsystems share one loaded dataset.",
  "concepts": [
    "FileMissionControlStore",
    "atomic write",
    "in-memory index",
    "JSON persistence",
    "singleton",
    "activity sequencing",
    "document versioning",
    "lazy import"
  ],
  "categories": [
    "mission-control",
    "storage",
    "persistence"
  ],
  "source_docs": [
    "e0411430c7984542"
  ],
  "backlinks": null,
  "word_count": 410,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`FileMissionControlStore` is the production storage backend for Mission Control. It persists all entities as JSON arrays in `~/.pocketpaw/mission_control/` and holds a full in-memory copy in Python dicts keyed by ID. Reads are always served from memory (O(1)); writes flush the changed entity type to disk immediately.

## Storage Layout

```
~/.pocketpaw/mission_control/
    agents.json
    tasks.json
    messages.json
    activities.json
    documents.json
    notifications.json
    projects.json
```

Each file is an independent array. This flat-file-per-type approach limits the write blast radius: saving one task does not require rewriting all agents.

## Atomic Writes

Every `_save_json()` call writes to a `.tmp` sibling file first, then renames it into place:

```python
def _save_json(self, path: Path, data: list[dict]) -> None:
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w") as f:
        json.dump(data, f, indent=2)
    temp_path.replace(path)  # atomic on POSIX
```

This prevents a partial write from leaving the JSON file truncated or malformed. `Path.replace()` maps to `rename(2)` on POSIX systems.

## In-Memory Indexing

All data is loaded into `dict[str, Entity]` on startup via `_load_all()`. List operations filter in Python over the in-memory collection rather than scanning files. This works well for the documented scale limit (< 10k records per type).

Activities get an additional `_activity_seq` dict and `_activity_counter` integer. ISO 8601 timestamps have one-second precision; multiple activities created in the same second would sort non-deterministically. The sequence counter acts as a tiebreaker:

```python
activities.sort(
    key=lambda a: (a.created_at, self._activity_seq.get(a.id, 0)),
    reverse=True,
)
```

## Document Versioning

`save_document()` auto-increments `version` on update:

```python
existing = self._documents.get(document.id)
if existing:
    document.version = existing.version + 1
```

This provides lightweight change tracking without a separate history table. The old version is overwritten — only the current version number is stored, not history.

## Lazy Circular Import

`_load_all()` imports `Project` from `pocketpaw.deep_work.models` inside the method body to avoid a circular import at module load time.

## Singleton Pattern

```python
def get_mission_control_store(base_path=None) -> FileMissionControlStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = FileMissionControlStore(base_path)
    return _store_instance
```

`reset_mission_control_store()` nulls the singleton for test isolation.

## Known Gaps

- **No concurrent write safety**: All writes are synchronous dict mutations followed by file writes. If two async tasks write simultaneously in the same event loop tick, the second write overwrites the first. Safety relies on the GIL and APScheduler's sequential job execution.
- **Scale ceiling**: Loading the full JSON file into memory becomes slow beyond ~10k records per type.
- **`get_stats()` uses runtime circular import**: It imports `ProjectStatus` inside the method body, a pattern that should be replaced with a top-level import.