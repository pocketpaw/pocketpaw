---
{
  "title": "Sessions API Router — CRUD and Runtime Session Management",
  "summary": "FastAPI router providing CRUD endpoints for chat sessions, plus specialized runtime endpoints that bridge PocketPaw's file-based session store with the cloud API. All endpoints require a valid enterprise license via the require_license dependency.",
  "concepts": [
    "FastAPI router",
    "session CRUD",
    "runtime sessions",
    "license gate",
    "history proxy",
    "session key formats",
    "activity tracking"
  ],
  "categories": [
    "cloud",
    "sessions",
    "API",
    "FastAPI"
  ],
  "source_docs": [
    "6ef9122bfcb3bbd3"
  ],
  "backlinks": null,
  "word_count": 384,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Sessions API Router — CRUD and Runtime Session Management

## Purpose

The sessions router exposes HTTP endpoints for managing chat sessions in PocketPaw Enterprise. It serves two distinct needs:

1. **Cloud CRUD** — standard create/read/update/delete for MongoDB-backed sessions, scoped by workspace and user
2. **Runtime bridge** — endpoints that read from PocketPaw's native file-based session store, allowing the cloud dashboard to display sessions that originated from local CLI or WebSocket usage

## Architecture

### License Gate

The entire router is gated by `Depends(require_license)`, meaning no session endpoints work without a valid enterprise license. This is applied at the router level rather than per-endpoint.

### Cloud CRUD Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/sessions` | Create a session (delegates to `SessionService.create`) |
| GET | `/sessions` | List user's sessions in workspace |
| GET | `/sessions/{id}` | Get single session |
| PATCH | `/sessions/{id}` | Update title or pocket link |
| DELETE | `/sessions/{id}` | Soft-delete (204) |

All CRUD endpoints extract `workspace_id` and `user_id` from JWT via FastAPI dependencies.

### Runtime Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/sessions/runtime` | List sessions from PocketPaw's file store |
| POST | `/sessions/runtime/create` | Generate a new runtime session key |

The runtime list endpoint directly accesses the memory manager's internal `_load_session_index()` method. This is a pragmatic shortcut — the memory manager doesn't expose a public session listing API, so the router reaches into private state.

### History Proxy

`GET /sessions/{id}/history` attempts multiple strategies to retrieve chat history:
1. Try the runtime memory manager with several key formats (`sid`, `sid` with `:` separator, `websocket:sid`)
2. Fall back to empty results

The key format guessing exists because session IDs are stored differently depending on whether the session originated from WebSocket, CLI, or cloud API. This is a known inconsistency in the session key format.

### Activity Tracking

`POST /sessions/{id}/touch` updates the `lastActivity` timestamp and increments `messageCount` — called by the WebSocket layer on each message.

## Known Gaps

- Runtime list endpoint accesses private `_store._load_session_index()` — fragile coupling to memory manager internals
- History endpoint tries three key formats in a try/except loop — indicates an unresolved session key format inconsistency
- Runtime create generates UUIDs inline rather than delegating to SessionService
