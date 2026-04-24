---
{
  "title": "Session Management REST Router",
  "summary": "Provides the REST endpoints for creating, listing, deleting, renaming, searching, and exporting conversation sessions. Session IDs are UUID-based slugs prefixed with `websocket_` to maintain compatibility with the WebSocket session key format used by the memory subsystem.",
  "concepts": [
    "session management",
    "FastAPI router",
    "UUID session ID",
    "session index",
    "memory manager",
    "delete 404 handling",
    "require_scope",
    "session listing",
    "WebSocket compatibility",
    "conversation history"
  ],
  "categories": [
    "api",
    "sessions",
    "memory",
    "rest-endpoints"
  ],
  "source_docs": [
    "3653ad7d8b381e6f"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Session Management REST Router

The sessions router exposes a clean REST API over PocketPaw's persistent conversation session store. It was extracted from `dashboard.py` where session operations were mixed with other WebSocket handler logic, and promoted to a dedicated v1 router for cleaner separation of concerns.

### Session ID Format

When a new session is created, the router generates an ID with the format `websocket_{12-char-uuid-hex}` — for example, `websocket_a3f7c9e21b44`. The `websocket_` prefix is not cosmetic. The memory manager's underlying store uses this prefix as a key namespace to distinguish session-scoped memory from other store entries. Generating this prefix in the REST creation path means REST-created sessions are indistinguishable from WebSocket-created ones at the storage layer, allowing a session started via REST to be continued over WebSocket without any migration step.

### Listing Sessions via the Fast Index

The list endpoint uses `store._load_session_index()` when available rather than scanning all session files. The index is a lightweight manifest kept updated by the store on every write — it trades a small amount of write overhead for near-instant list responses even when hundreds of sessions exist. The hasattr guard (`if hasattr(store, "_load_session_index")`) is a compatibility shim: older or alternative store implementations that lack the index method fall back to returning an empty list rather than crashing. This silences startup errors when the store has not yet been migrated.

Sessions in the index are sorted by `last_activity` descending, so the most recently active conversations appear first. The `limit` parameter (default 50, max 500) prevents accidental full-table scans when listing from large stores.

### Delete and 404 Handling

Delete delegates to `store.delete_session()` and inspects the boolean return value. Returning `False` means the session did not exist, and the router translates that into a 404 response. This distinction matters because a client that retries a failed delete request should not receive a 500 on the second attempt — the 404 tells it the operation has already been completed.

### Scope Guard

The entire router is mounted with `dependencies=[Depends(require_scope("sessions"))]`. Every endpoint in this file requires the `sessions` scope in the caller's access token. This is intentional: session content may include sensitive conversation history, and listing or deleting sessions should be available only to authenticated dashboard users or API clients that have been explicitly granted the scope.

### Integration Pattern

```python
# Create a session
POST /api/v1/sessions
# → {"id": "websocket_a3f7c9e21b44", "title": "New Chat"}

# List recent sessions
GET /api/v1/sessions?limit=20
# → {"sessions": [...], "total": 42}

# Delete a session
DELETE /api/v1/sessions/websocket_a3f7c9e21b44
```

### Known Gaps

The `update_session_title` endpoint and session search / export endpoints visible in the AST are not shown in the extracted source snippet — they were present in the original `dashboard.py` but may not have been fully migrated in the extracted file. Title updates and search are important for power users who manage many sessions. The hasattr guard on `_load_session_index` is also a signal that the store interface is not fully stabilized — a formal `SessionStore` protocol with required methods would remove the need for runtime duck-typing.