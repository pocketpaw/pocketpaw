---
{
  "title": "Mission Control Storage Protocol Interface",
  "summary": "MissionControlStoreProtocol defines the abstract interface all Mission Control storage backends must implement using Python's structural Protocol pattern with runtime_checkable, enabling dependency injection and future backend swappability across all six entity types.",
  "concepts": [
    "MissionControlStoreProtocol",
    "Protocol",
    "runtime_checkable",
    "storage backend",
    "dependency injection",
    "TYPE_CHECKING",
    "Deep Work",
    "async interface"
  ],
  "categories": [
    "mission-control",
    "architecture",
    "storage"
  ],
  "source_docs": [
    "b4affc242c8b201c"
  ],
  "backlinks": null,
  "word_count": 328,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MissionControlStoreProtocol` is the contract layer between Mission Control's business logic and its persistence backends. It follows PocketPaw's protocol-first design (also used in `MemoryStoreProtocol`), where a `typing.Protocol` class defines the async interface that any concrete store must satisfy.

## Why Protocol-First?

Without a formal protocol, `MissionControlManager` would be tightly coupled to `FileMissionControlStore`. This creates a problem when:
1. Tests need to inject an in-memory mock store that does not write to disk.
2. Production deployments want a remote store (SQLite, PostgreSQL, Convex).
3. Multiple store implementations need to coexist.

The protocol decouples the manager from the concrete implementation. Any class that implements all the methods satisfies the protocol check because `@runtime_checkable` is applied:

```python
@runtime_checkable
class MissionControlStoreProtocol(Protocol):
    ...
```

`@runtime_checkable` allows `isinstance(store, MissionControlStoreProtocol)` checks at runtime, useful in factory functions that accept either a store instance or `None`.

## Interface Coverage

| Section | Key Methods |
|---------|------------|
| Projects | `save_project`, `get_project`, `list_projects`, `delete_project` |
| Agents | `save_agent`, `list_agents`, `delete_agent`, `update_agent_heartbeat` |
| Tasks | `save_task`, `get_task`, `list_tasks`, `delete_task`, `get_tasks_for_agent`, `get_blocked_tasks` |
| Messages | `save_message`, `get_messages_for_task`, `delete_message` |
| Activities | `save_activity`, `get_activities`, `get_activity_feed` |
| Documents | `save_document`, `list_documents`, `delete_document` |
| Notifications | `save_notification`, `get_notifications_for_agent`, `get_undelivered_notifications`, `mark_notification_delivered`, `mark_notification_read` |
| Utility | `clear_all` |

The dual notification query methods serve different consumers: the heartbeat daemon uses `get_undelivered_notifications` to find items to push, while the dashboard uses `get_notifications_for_agent` with `unread_only=True` to show the inbox.

## TYPE_CHECKING Guard

The `Project` type is imported under `TYPE_CHECKING` to avoid a circular import:

```python
if TYPE_CHECKING:
    from pocketpaw.deep_work.models import Project
```

At runtime, `Project` is referenced only in method signatures. Python's Protocol checks structurally at runtime, not via import, so this is safe.

## Known Gaps

- **No filtering protocol for projects**: `list_projects` accepts `status` and `limit` but other filter dimensions (tags, creator) are not part of the contract.
- **No batch operations**: The protocol has no bulk-insert or bulk-update methods. Each entity must be saved individually.