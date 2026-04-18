---
{
  "title": "Session Service — Business Logic for Session Lifecycle Management",
  "summary": "Stateless service class encapsulating all session business logic including CRUD, pocket-scoped queries, history retrieval from multiple backends, and activity tracking. Enforces ownership checks and emits domain events on session creation.",
  "concepts": [
    "SessionService",
    "upsert",
    "ownership enforcement",
    "soft delete",
    "dual lookup",
    "event bus",
    "history retrieval",
    "activity tracking",
    "session key format"
  ],
  "categories": [
    "cloud",
    "sessions",
    "business logic",
    "services"
  ],
  "source_docs": [
    "2c70dfccf21992f9"
  ],
  "backlinks": null,
  "word_count": 467,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Session Service — Business Logic for Session Lifecycle Management

## Purpose

`SessionService` centralizes all session business logic away from the router layer. It handles CRUD operations, ownership enforcement, event emission, and the complexity of retrieving history from multiple storage backends (MongoDB messages and runtime file memory).

## Key Operations

### Create (Idempotent Upsert)

`create()` implements an upsert pattern: if a `session_id` already exists in MongoDB, it updates the existing record (adding pocket links, updating title) rather than creating a duplicate. This prevents the common scenario where a runtime session is "adopted" into cloud multiple times.

A new session triggers a `session.created` event on the event bus, enabling cross-domain reactions without tight coupling.

### List and Get

`list_sessions()` returns all non-deleted sessions for a workspace/user pair, sorted by `lastActivity` descending. The soft-delete filter uses `Session.deleted_at == None` (with a `noqa: E711` because Beanie requires `==` for MongoDB queries, not `is`).

### Ownership Enforcement

`_get_session()` is the internal fetch method used by get/update/delete. It implements a dual-lookup strategy:
1. Try to parse the ID as a MongoDB `PydanticObjectId`
2. Fall back to querying by `sessionId` field

This handles both ObjectId-based and string-based session references. After fetching, it enforces:
- **Existence** — raises `NotFound` if session is missing or soft-deleted
- **Ownership** — raises `Forbidden` if the requesting user is not the session owner

### Soft Delete

`delete()` sets `deleted_at` to the current UTC timestamp rather than removing the document. This preserves session history and enables potential recovery.

### Pocket-Scoped Operations

`list_for_pocket()` and `create_for_pocket()` provide pocket-contextualized session access. `create_for_pocket` wraps the standard create with the pocket_id pre-set.

### History Retrieval

`get_history()` attempts two backends in order:
1. **Cloud Messages** (if session has a group) — queries the `Message` collection for group chat messages
2. **Runtime file memory** — instantiates a `MemoryManager` and tries multiple session key formats

The key format guessing (`sid`, `sid.replace("_", ":", 1)`, `f"websocket:{sid}"`) works around the inconsistent session key format between cloud and runtime layers.

### Touch (Activity Tracking)

`touch()` updates `lastActivity` and increments `messageCount`. It also implements a fallback: if the session isn't found by full ID, it strips the `websocket_` prefix and retries. This handles the case where the WebSocket layer sends the full prefixed ID but the session was stored without it.

## Response Serialization

`_session_response()` converts Beanie `Session` documents to frontend-friendly dicts with camelCase keys and ISO-formatted datetimes. This manual serialization exists because the frontend expects a specific shape that doesn't match the Beanie model directly.

## Known Gaps

- `get_history()` instantiates a new `MemoryManager()` on every call rather than reusing a shared instance
- Session key format inconsistency requires try/except loops in multiple places
- `touch()` fallback strips exactly 10 characters (`websocket_`) — hardcoded prefix length is fragile
- No pagination on `list_sessions()` — could return unbounded results for active users
