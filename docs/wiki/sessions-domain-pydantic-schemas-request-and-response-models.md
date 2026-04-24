---
{
  "title": "Sessions Domain Pydantic Schemas: Request and Response Models",
  "summary": "This file defines the three Pydantic models for the sessions domain: CreateSessionRequest, UpdateSessionRequest, and SessionResponse. The schemas are intentionally lean, supporting optional linkage to pockets, groups, agents, and existing runtime sessions at creation time.",
  "concepts": [
    "Pydantic",
    "CreateSessionRequest",
    "UpdateSessionRequest",
    "SessionResponse",
    "session_id",
    "pocket linking",
    "group session",
    "agent DM",
    "runtime session upgrade",
    "soft delete",
    "PATCH semantics"
  ],
  "categories": [
    "sessions",
    "schemas",
    "Pydantic",
    "EE cloud"
  ],
  "source_docs": [
    "58d6c515fe269c31"
  ],
  "backlinks": null,
  "word_count": 424,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/sessions/schemas.py` is the data contract layer for the sessions domain. It defines what the HTTP layer accepts and what it guarantees to return, independently of the MongoDB document model and the service logic.

## CreateSessionRequest

```python
class CreateSessionRequest(BaseModel):
    title: str = "New Chat"
    pocket_id: str | None = None
    group_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
```

Most fields are optional because sessions can be created in several different contexts:

- A plain chat session: only `title` is provided.
- A pocket-linked session: `pocket_id` provided; the service links the session to the pocket.
- A group session: `group_id` provided; messages are stored under the group context.
- A DM session with an agent: `agent_id` provided; used to look up DM history.
- A runtime session upgrade: `session_id` provided (e.g., `"websocket_abc123"`); the service upserts rather than creating a new document, linking the in-memory session to MongoDB.

The `session_id` field is particularly important for the runtime session workflow: it allows the frontend to first create a lightweight runtime session (which returns a key immediately) and later "claim" it by persisting it to MongoDB with a pocket or agent link.

## UpdateSessionRequest

All fields are optional for PATCH semantics. The `pocket_id` field allows the frontend to link or unlink a pocket from an existing session — a common operation when a user drags a session into a pocket in the UI.

## SessionResponse

The response model reflects the full MongoDB-backed session shape:

```python
class SessionResponse(BaseModel):
    id: str
    session_id: str  # The unique sessionId
    workspace: str
    owner: str
    title: str
    pocket: str | None
    group: str | None
    agent: str | None
    message_count: int
    last_activity: datetime
    created_at: datetime
    deleted_at: datetime | None = None
```

The `id` field is the MongoDB ObjectId (string), while `session_id` is the human-readable unique key (e.g., `websocket_abc123`). Both are exposed because different parts of the system use each: the router uses ObjectId for REST path parameters, while the memory manager and WebSocket infrastructure use the string `sessionId`.

`deleted_at` is optional and defaults to `None`. Non-null `deleted_at` means the session is soft-deleted and should be filtered from active session lists.

## Known Gaps

- **No pagination fields in response**: The list endpoint returns a flat array. There are no cursor, offset, or total-count fields in the response schema for large session lists.
- **No context_type field**: The `SessionResponse` does not expose `context_type` ("group" vs "pocket"), so the frontend cannot tell from the response alone which message retrieval path `get_history` will use.