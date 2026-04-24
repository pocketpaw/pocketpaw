---
{
  "title": "AudienceResolver: Event-to-Recipient Mapping with Short-TTL Caching",
  "summary": "AudienceResolver translates a typed event into the list of user IDs that should receive it over WebSocket, applying domain-specific fan-out rules for groups, messages, workspace membership, sessions, agent streams, notifications, and presence. Member lookups are cached with a configurable short TTL to avoid hammering the database on high-frequency events, and the cache can be invalidated selectively when membership changes.",
  "concepts": [
    "AudienceResolver",
    "event routing",
    "fan-out",
    "caching",
    "TTL",
    "group members",
    "workspace members",
    "MemberFetcher",
    "cache invalidation",
    "presence routing",
    "typing events",
    "WebSocket audience"
  ],
  "categories": [
    "realtime",
    "audience resolution",
    "caching",
    "EE cloud"
  ],
  "source_docs": [
    "598ce4c442c003b1"
  ],
  "backlinks": null,
  "word_count": 575,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/realtime/audience.py` answers a single question for every event: "who should receive this?". The answer depends entirely on the event type and the data it carries — a `message.new` event goes to all group members except the sender; a `notification.new` event goes to exactly one user; a `workspace.member_added` event goes to all current workspace members plus the newly-added user.

## Why Not Route by Room?

A common alternative is purely room-based routing: every connected client joins a "room" and the server broadcasts to the room. The `AudienceResolver` approach is used instead because different event types have different audience shapes even within the same domain. `group.joined` should only go to the newly-added user (existing members already have the room state), while `group.member_added` goes to all current members. Room-based routing cannot express this distinction without client-side filtering of irrelevant events.

## Caching

Member list lookups are the primary database hotspot in a realtime system. A high-volume chat room may emit dozens of events per second, each of which naively requires a `SELECT * FROM group_members WHERE group_id = ?`. The resolver avoids this by caching lookup results in a `dict[(kind, key), (timestamp, results)]` with a default TTL of 2 seconds:

```python
async def _cached(self, kind: str, key: str, fn: MemberFetcher | None) -> list[str]:
    now = time.monotonic()
    entry = self._cache.get((kind, key))
    if entry and now - entry[0] < self._ttl:
        return list(entry[1])
    value = await fn(key)
    self._cache[(kind, key)] = (now, value)
    return list(value)
```

The cache returns a copy (`list(entry[1])`) so callers that modify the returned list do not corrupt the cache entry.

## Cache Invalidation

Three invalidation methods allow the cache to be cleared promptly when membership changes:

- `invalidate_group(group_id)` — called when a group member list changes.
- `invalidate_workspace(workspace_id)` — clears workspace member and admin caches.
- `invalidate_user_peers(user_id)` — clears the workspace-peer cache for a specific user.

The docstring on `invalidate_workspace` notes explicitly that peer caches are user-scoped (keyed by `user_id`, not `workspace_id`) and are not cleared here — they either expire on TTL or are cleared via `invalidate_user_peers`. This prevents a subtle bug where clearing a workspace-scoped cache entry using the wrong key would silently leave stale peer data.

## Audience Rules by Event Domain

**Groups**: Most events fan out to all current group members via `_group()`. `group.joined` is an exception — it only goes to the newly-added user(s) because it carries full room hydration data that existing members do not need. `group.member_removed` includes the removed user in the audience so their client can react (e.g., remove the room from the sidebar).

**Messages**: `message.new` excludes the sender since the sender's own client handles the echo locally. `message.sent` goes only to the sender as a delivery confirmation.

**Workspace invites**: These route to workspace admins plus the invited user, rather than the full member list, to preserve invite confidentiality.

**Sessions**: Events route to both the session owner and the peer (e.g., the agent).

**Presence**: Presence events route to the user's workspace peers — other users in the same workspace who should see the online/offline indicator.

**Typing**: Typing events (`typing.start`, `typing.stop`) are excluded from resolver routing entirely; the `ConnectionManager` routes them directly to the room.

## Known Gaps

- **No persistence**: The cache is entirely in-process. In a multi-instance deployment the cache on instance A does not know about invalidations on instance B.
- **Fixed fan-out strategy**: There is no pluggable strategy for extending audience rules. Adding a new event type requires modifying the `audience` method directly.