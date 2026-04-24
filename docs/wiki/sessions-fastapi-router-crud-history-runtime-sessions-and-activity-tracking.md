---
{
  "title": "Sessions FastAPI Router: CRUD, History, Runtime Sessions, and Activity Tracking",
  "summary": "This is the FastAPI router for the sessions domain, providing endpoints to create, list, fetch, update, and delete user chat sessions, retrieve message history, and manage lightweight runtime sessions that are not persisted to MongoDB. A notable design is the runtime session endpoints that bridge the in-memory memory manager with the persistent session store.",
  "concepts": [
    "sessions router",
    "FastAPI",
    "runtime sessions",
    "session history",
    "touch endpoint",
    "MongoMemoryStore",
    "FileMemoryStore",
    "duck typing",
    "soft delete",
    "agent DM sessions",
    "require_license",
    "session index"
  ],
  "categories": [
    "sessions",
    "routing",
    "EE cloud",
    "runtime sessions"
  ],
  "source_docs": [
    "f55bb57685edcb9f"
  ],
  "backlinks": null,
  "word_count": 485,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/sessions/router.py` defines the HTTP boundary for the sessions domain. All routes inherit `Depends(require_license)` from the `APIRouter` declaration. The router covers two distinct kinds of sessions: persistent MongoDB-backed sessions and ephemeral runtime sessions that live only in the memory manager's session index.

## Persistent Session CRUD

The standard CRUD routes delegate to `SessionService`:

- `POST /sessions` — Creates a session, or upserts if a `session_id` matching an existing runtime session is provided. Permission-gated by `require_action_any_workspace("session.read_own")`.
- `GET /sessions` — Lists the caller's sessions. Accepts an optional `?agent_id=X` query parameter to filter to DM sessions for a specific agent, which the frontend uses to resolve the DM room for that agent without fetching all sessions.
- `GET /sessions/{session_id}` — Fetch a single session by ObjectId or sessionId.
- `PATCH /sessions/{session_id}` — Update title or pocket link.
- `DELETE /sessions/{session_id}` — Soft-delete (sets `deleted_at`).

## Session History

The history endpoint at `GET /sessions/{session_id}/history` proxies through `SessionService.get_history`, which reads from the unified MongoDB messages store. It handles the `NotFound` exception by returning an empty message list rather than a 404:

```python
try:
    return await SessionService.get_history(session_id, user_id, limit=limit)
except NotFound:
    return {"messages": []}
```

This defensive choice means the frontend never gets an error when fetching history for a newly-created session that has no messages yet.

## Runtime Session Endpoints

Two endpoints provide a lightweight session management surface for in-memory runtime sessions:

**`GET /sessions/runtime`** — Reads the memory manager's session index, which is an in-process dict maintained by the active `MemoryStore`. The implementation dispatches between async (`MongoMemoryStore._load_session_index_async`) and sync (`FileMemoryStore._load_session_index`) variants via duck typing (`hasattr` checks), returning empty if neither is available. Sessions are sorted by `last_activity` descending and truncated at `limit` (default 50).

**`POST /sessions/runtime/create`** — Creates a new session key in the format `websocket_{12-hex-chars}` using `uuid.uuid4().hex[:12]`. This does not create a MongoDB document; it only returns the key for the client to use. The client can later call `POST /sessions` with this key as `session_id` to persist the session.

This two-step pattern separates the concerns of key generation (instant, no DB) from session persistence (asynchronous, DB-backed) and allows clients to start a chat immediately without waiting for a write.

## Touch

`POST /sessions/{session_id}/touch` (204 No Content) updates the session's `lastActivity` timestamp and increments `messageCount`. It is called by the message pipeline after each message delivery to keep session metadata current. The endpoint has no auth dependency — it is treated as an internal heartbeat call.

## Known Gaps

- **Runtime session index is per-process**: `GET /sessions/runtime` reads from an in-process store. In a multi-instance deployment, runtime sessions on one instance are invisible to others.
- **Duck-typed dispatch**: The `hasattr` dispatch in `list_runtime_sessions` is fragile — a store that has neither method returns empty silently rather than raising an informative error.
- **Touch has no auth**: The `touch` endpoint has no authentication dependency, meaning any unauthenticated caller who knows a session ID can bump its activity timestamp.