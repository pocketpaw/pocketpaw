---
{
  "title": "AudienceResolver Behavioral Tests: Fan-Out Logic, Caching, and Edge Cases",
  "summary": "These tests verify the complete behavioral contract of AudienceResolver — determining which user IDs receive each event type, including removed-member inclusion, invite routing to admins plus invitee, TTL caching with manual invalidation, session deduplication, and empty-list fallback for unknown event types.",
  "concepts": [
    "AudienceResolver",
    "event routing",
    "fan-out",
    "cache TTL",
    "cache invalidation",
    "group members",
    "workspace admins",
    "session deduplication",
    "invite routing",
    "WebSocket",
    "unknown event fallback"
  ],
  "categories": [
    "testing",
    "real-time",
    "event routing",
    "caching",
    "test"
  ],
  "source_docs": [
    "93c6d64064efe8de"
  ],
  "backlinks": null,
  "word_count": 340,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`AudienceResolver` is the routing brain of the real-time event bus. For every published event, it answers which users should receive it. The answer differs by event type: group events go to all members, notification events go to a single recipient, session events go to both participants. This file covers all documented routing rules.

## Event-Specific Routing Rules

### Group Created
```python
ev = GroupCreated(data={"group_id": "g1", "member_ids": ["u1", "u2", "u3"]})
assert set(await r.audience(ev)) == {"u1", "u2", "u3"}
```
`GroupCreated` uses the payload's `member_ids` directly, avoiding a database lookup for a newly created group that might not yet be queryable.

### Removed Member Inclusion
```python
ev = GroupMemberRemoved(data={"group_id": "g1", "user_id": "u3"})
assert set(await r.audience(ev)) == {"u1", "u2", "u3"}
```
The removed user must receive the event so their client can close the group channel. Without this, the removed user's session would remain open to a channel they no longer have access to.

### Notification Targeting
Notifications are private — only the recipient should receive them. `NotificationNew` resolves to a single-element list.

### Invite Routing
Invites go to workspace admins plus the invitee if they already have a user account. If the invitee is not yet a registered user, admins alone receive the event.

## TTL Caching and Invalidation

```python
await r.audience(u); await r.audience(u)
assert calls["n"] == 1, "second call within TTL should hit cache"
r.invalidate_group("g1")
await r.audience(u)
assert calls["n"] == 2
```

The resolver caches group and workspace member lists to avoid redundant database queries on high-frequency events. Manual invalidation (`invalidate_group`, `invalidate_user_peers`) is provided for use after membership changes.

## Session Deduplication

```python
ev = SessionUpdated(data={"session_id": "s1", "user_id": "u1", "peer_id": "u1"})
assert await r.audience(ev) == ["u1"]
```

When a user is both session owner and peer (e.g., a solo AI session), deduplication prevents duplicate WebSocket messages.

## Unknown Event Fallback

```python
assert await r.audience(Event(type="something.made.up", data={})) == []
```

Unknown event types return an empty list rather than raising. A new event type added without an audience branch fails silently rather than breaking all event processing.

## Known Gaps

None identified.