---
{
  "title": "Sessions Domain Pydantic Schemas",
  "summary": "Request and response Pydantic models for the sessions API. Defines CreateSessionRequest (with optional pocket/group/agent linking), UpdateSessionRequest, and SessionResponse for frontend consumption.",
  "concepts": [
    "Pydantic schemas",
    "CreateSessionRequest",
    "UpdateSessionRequest",
    "SessionResponse",
    "session linking",
    "soft delete"
  ],
  "categories": [
    "cloud",
    "sessions",
    "schemas",
    "data models"
  ],
  "source_docs": [
    "6f5fb0cc0466bdd5"
  ],
  "backlinks": null,
  "word_count": 217,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Sessions Domain Pydantic Schemas

## Purpose

These Pydantic models define the contract between the sessions API and its consumers (primarily the cloud dashboard frontend). They validate incoming requests and structure outgoing responses.

## Models

### CreateSessionRequest

Fields:
- `title` (str, default `"New Chat"`) — display name for the session
- `pocket_id` (optional) — link session to a pocket on creation
- `group_id` (optional) — link to a chat group
- `agent_id` (optional) — link to a specific agent
- `session_id` (optional) — link to an existing runtime session (e.g., `"websocket_abc123"`)

The `session_id` field enables the cloud layer to adopt an already-running runtime session into MongoDB. When present, the service checks for an existing record and updates it rather than creating a duplicate.

### UpdateSessionRequest

Only `title` and `pocket_id` are updatable. Both are optional — only provided fields are applied.

### SessionResponse

Full session representation for API responses. Notable fields:
- `session_id` — the unique session identifier (distinct from `id` which is the MongoDB ObjectId)
- `pocket` / `group` / `agent` — optional foreign key links
- `deleted_at` — soft-delete marker (nullable)

## Design Notes

The dual ID pattern (`id` for MongoDB ObjectId, `session_id` for the application-level identifier) exists because runtime sessions use human-readable keys like `websocket_abc123`, while MongoDB assigns its own ObjectIds. Both must be tracked.
