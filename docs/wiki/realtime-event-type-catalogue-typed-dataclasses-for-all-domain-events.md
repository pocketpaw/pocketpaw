---
{
  "title": "Realtime Event Type Catalogue: Typed Dataclasses for All Domain Events",
  "summary": "This module defines the complete catalogue of realtime events as Python dataclasses, covering workspace lifecycle, group and message operations, presence, typing, file uploads, sessions, agent streaming, and notifications. Each concrete event subclass declares a `ClassVar EVENT_TYPE` string that is automatically applied in `__post_init__`, ensuring the `type` field is always set correctly without manual assignment at every call site.",
  "concepts": [
    "Event dataclass",
    "ClassVar EVENT_TYPE",
    "__post_init__",
    "GroupJoined",
    "GroupMemberAdded",
    "agent streaming events",
    "typing events",
    "presence events",
    "session events",
    "workspace events",
    "message events",
    "notification events",
    "realtime catalogue"
  ],
  "categories": [
    "realtime",
    "events",
    "dataclasses",
    "EE cloud"
  ],
  "source_docs": [
    "ab807f9957a9b219"
  ],
  "backlinks": null,
  "word_count": 491,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/realtime/events.py` is the type catalogue for the realtime system. Every event that can be emitted over WebSocket has a corresponding dataclass here. The file deliberately contains no business logic — it is a pure data definition layer that both the emitters (service methods) and the router (AudienceResolver, InProcessBus) depend on.

## Base Event Design

The `Event` base class uses `__post_init__` to automatically set `self.type` from the subclass's `EVENT_TYPE` ClassVar:

```python
@dataclass
class Event:
    type: str = ""
    data: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        cls_type = getattr(type(self), "EVENT_TYPE", "")
        if cls_type:
            self.type = cls_type
```

This pattern prevents the error class of a developer creating, say, `MessageNew(data={...})` and forgetting to pass `type="message.new"`. The `ClassVar` annotation also ensures `EVENT_TYPE` is not treated as a dataclass field — it is class-level metadata, not instance data.

The `ts` field defaults to the current UTC time at construction. This means the event carries its own timestamp independent of when it is consumed by the bus or delivered to the client.

## Event Domains

The catalogue covers seven distinct domains:

**Workspace** (8 events): lifecycle (`updated`, `deleted`), membership (`member_added`, `member_removed`, `member_role`), and invite workflow (`invite.created`, `invite.accepted`, `invite.revoked`).

**Groups** (12 events): lifecycle, membership, agent assignment, pinning, and unread delta tracking.

**Messages** (9 events): new messages, edit, delete, reactions, read receipts, and thread replies.

**Presence and typing** (4 events): `presence.online`, `presence.offline`, `typing.start`, `typing.stop`. Typing events are routed by the ConnectionManager directly and are excluded from the AudienceResolver's fan-out logic.

**Files** (2 events): `file.ready` and `file.deleted`.

**Sessions** (3 events): created, updated, deleted.

**Agent streaming** (8 events): `agent.thinking`, `agent.tool_start`, `agent.tool_result`, `agent.error`, `agent.stream_start`, `agent.stream_chunk`, `agent.stream_end`, `agent.tool_use`. These provide granular visibility into agent execution for streaming UIs.

**Notifications** (3 events): new, read, cleared.

## GroupJoined vs GroupMemberAdded

The docstring on `GroupJoined` captures a subtle but important distinction:

> "Full group payload delivered only to a newly-added user. Lets the recipient's sidebar insert the room without a manual refresh. Existing members already have the room and receive `GroupMemberAdded` instead."

This means the AudienceResolver must route these two events differently even though they look similar — `GroupJoined` goes only to the new user(s), `GroupMemberAdded` goes to all current members.

## Usage Pattern

Emitters construct events using the typed subclasses:

```python
await emit(SessionCreated(data={"session_id": str(session.id), "user_id": user_id}))
```

The AudienceResolver and InProcessBus work with `event.type` (a string) for routing, not the class type. This means new event types can be added by creating a subclass and adding a branch in `AudienceResolver.audience` — no changes needed in the bus layer.

## Known Gaps

- **Untyped `data` dict**: All events carry a `data: dict` with no schema. A typo in a data key (e.g., `"user_i"` instead of `"user_id"`) will silently produce wrong behaviour in the audience resolver without any validation error.
- **No version field on Event**: If the event schema changes in a breaking way, there is no version discriminator to allow consumers to handle old and new formats simultaneously.