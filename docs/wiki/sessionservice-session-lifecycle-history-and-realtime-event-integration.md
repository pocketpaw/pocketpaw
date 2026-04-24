---
{
  "title": "SessionService: Session Lifecycle, History, and Realtime Event Integration",
  "summary": "SessionService is the stateless service class handling the full lifecycle of chat sessions — creating with upsert support for runtime sessions, listing by agent or pocket, retrieving message history from MongoDB, soft-deleting, and emitting realtime events after each mutation. A touch method provides a lightweight activity heartbeat that increments message count and updates lastActivity.",
  "concepts": [
    "SessionService",
    "upsert",
    "runtime session",
    "session history",
    "touch heartbeat",
    "soft delete",
    "ObjectId fallback",
    "realtime emit",
    "event_bus",
    "context_type",
    "group messages",
    "pocket messages",
    "Beanie ODM"
  ],
  "categories": [
    "sessions",
    "service layer",
    "EE cloud",
    "realtime"
  ],
  "source_docs": [
    "843b17e1b90a7eff"
  ],
  "backlinks": null,
  "word_count": 526,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/sessions/service.py` encapsulates all session business logic. Like `PocketService`, it is a class of static async methods with no instance state, making it safe to use from any async context without instantiation.

## Create with Upsert

The `create` method handles two scenarios. When no `session_id` is provided, it generates one using `f"websocket_{uuid.uuid4().hex[:12]}"` and inserts a new `Session` document. When a `session_id` is provided and a matching MongoDB document already exists, it _updates_ the existing document rather than creating a duplicate:

```python
if body.session_id:
    existing = await Session.find_one(Session.sessionId == body.session_id)
    if existing:
        # update instead of insert
        ...
        return _session_response(existing)
```

This upsert pattern is critical for the runtime session upgrade workflow: the frontend creates a `websocket_*` key in-memory, starts chatting, and later calls `POST /sessions` with that key to attach a pocket or agent link. Without the upsert, a second call with the same key would create a duplicate session document.

After a successful create, the method emits both a legacy `event_bus.emit("session.created", ...)` call and the typed `SessionCreated` realtime event. The dual emission bridges the older `shared.events` bus (used for internal hooks) and the newer realtime `emit` system (used for WebSocket fan-out).

## Session Lookup: ObjectId and sessionId Fallback

`_get_session` attempts to fetch by MongoDB ObjectId first, then falls back to `Session.sessionId`:

```python
try:
    session = await Session.get(PydanticObjectId(session_id))
except Exception:
    session = await Session.find_one(Session.sessionId == session_id)
```

This handles the case where callers use either the MongoDB ObjectId or the human-readable `sessionId` string as the path parameter. The exception-based dispatch (rather than `isinstance` checking) is necessary because `PydanticObjectId()` raises rather than returning `None` for invalid ObjectId strings.

## Message History

`get_history` queries the `Message` collection using two different filters depending on `context_type`:

- **Group context**: Filters by `group` field, returning messages regardless of session.
- **Pocket context**: Filters by `session_key` matching `session.sessionId`.

This dual path exists because messages in group chat are stored at the group level (multiple sessions can participate in the same group), while pocket messages are keyed to the specific session.

## Touch

The `touch` method is designed for resilience. It first looks up the session by `sessionId`, then falls back to stripping the `websocket_` prefix and trying again. This handles the case where the caller passes a prefixed key but the stored key is unprefixed:

```python
if not session and session_id.startswith("websocket_"):
    session = await Session.find_one(Session.sessionId == session_id[10:])
```

If no session is found by either path, `touch` returns silently rather than raising. This is intentional: touch is called by the message pipeline as a best-effort activity update, and a missing session should not abort message delivery.

## Soft Delete

Sessions are soft-deleted by setting `deleted_at = datetime.now(UTC)` and saving. The document remains in MongoDB and `_get_session` treats a non-null `deleted_at` as equivalent to not found. Hard deletion is not implemented.

## Known Gaps

- **No history pagination cursor**: `get_history` uses a `limit` parameter but no offset or cursor, so it is not possible to page through long histories.
- **Dual event emission**: The `create` method emits to both `event_bus` (legacy) and the realtime `emit` function. This creates two code paths that must be kept in sync if the session schema changes.