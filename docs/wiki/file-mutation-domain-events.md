---
{
  "title": "File Mutation Domain Events",
  "summary": "Declares the Pydantic event shapes published to the `ee.cloud.realtime` bus whenever a file is added, updated, removed, or moved. These schemas form the Phase 1-2 contract between the files subsystem and future realtime subscribers, ensuring the event bus wire format is stable before the subscriber (Phase 4) is built.",
  "concepts": [
    "FileAdded",
    "FileUpdated",
    "FileRemoved",
    "FileMoved",
    "FileEntry",
    "domain events",
    "realtime bus",
    "event-driven architecture",
    "pub/sub",
    "Phase 4 realtime bridge",
    "schema-first design"
  ],
  "categories": [
    "files",
    "events",
    "realtime",
    "cloud"
  ],
  "source_docs": [
    "0030c5c198932c9c"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee.cloud.files.events` module defines the domain event vocabulary for the files subsystem. Providers emit these events after performing mutations; a realtime bridge -- planned for Phase 4 -- will subscribe to them on the `ee.cloud.realtime` bus and forward them to connected WebSocket clients. By defining the event shapes now, Phase 1-2 work can publish events without any subscriber present, and Phase 4 can wire the bridge without renegotiating the schema.

## Why a Separate Events Layer?

Without dedicated event types, providers would need to know about the realtime transport to push updates, coupling domain logic to infrastructure. The event module breaks that dependency: providers call `bus.publish(FileAdded(entry=entry))` and nothing else. The bus, transport, and subscriber implementations are free to evolve independently.

This pattern also enables future audit logging, search index invalidation, and cache busting by adding new subscribers without modifying provider code.

## Event Shapes

### FileAdded
Published after a successful upload. Carries the complete `FileEntry` so subscribers receive all metadata (ID, path, mime type, size, permissions) in a single message. Subscribers do not need to call back into the files API to hydrate the entry.

### FileUpdated
Published after a rename or replace operation. Also carries the full `FileEntry` reflecting the post-mutation state. Subscribers can use the entry's ID to correlate with any previously cached version.

### FileRemoved
Published after a delete. Unlike `FileAdded` and `FileUpdated`, this event does not carry a `FileEntry` because the entry no longer exists. Instead it carries the minimum identifiers needed for cache eviction and UI removal: `id`, `workspace_id`, and `provider_id`. The `workspace_id` is optional (nullable) because personal-scope files may not belong to a specific workspace.

### FileMoved
Published after a cross-mount or within-mount move. Carries both the updated `FileEntry` (with the new `mount_path`) and `old_path` so subscribers can remove stale cache entries keyed on the previous path without a secondary lookup.

## Relationship to FileEntry

All events that carry an entry import `FileEntry` from `ee.cloud.files.schemas`. This ensures events share the same validated, normalised representation used everywhere else in the files module -- no separate DTO mapping is required.

## Phase Gating

The module docstring explicitly notes that Phase 1-2 only defines shapes. No bus wiring, no publish calls, and no subscribers exist yet. This is an intentional deferral: adding event emission to providers before the bus infrastructure exists would require mocking that infrastructure in every provider test. The shapes-first approach lets teams agree on the contract in code review before implementation begins.

## Known Gaps

- **No `FileCopied` event.** Cross-scope moves are rejected by the error layer today; when copy-then-delete is implemented, a new event type will be needed.
- **No event versioning.** If `FileEntry` fields change, all consumers break simultaneously. A version field or envelope wrapper would allow gradual migration.
- **Bus publish calls are absent.** Providers do not yet emit these events. The shapes exist but are not wired into any mutation path.