---
{
  "title": "Session Schema Tests: Create, Update, and Response Contract Validation",
  "summary": "Unit tests for the three Pydantic schemas governing session lifecycle in PocketPaw's EE cloud layer — `CreateSessionRequest`, `UpdateSessionRequest`, and `SessionResponse`. The tests pin default values, optional-field behavior, and the nullable `deleted_at` field used for soft-deletion semantics.",
  "concepts": [
    "CreateSessionRequest",
    "UpdateSessionRequest",
    "SessionResponse",
    "Pydantic schema",
    "session lifecycle",
    "soft deletion",
    "deleted_at",
    "pocket link",
    "group",
    "agent",
    "default values"
  ],
  "categories": [
    "testing",
    "schemas",
    "sessions",
    "cloud API",
    "test"
  ],
  "source_docs": [
    "d8e31501214b183a"
  ],
  "backlinks": null,
  "word_count": 380,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Sessions in PocketPaw represent chat conversations. Each session can optionally be linked to a pocket (dashboard), a group, and an agent. The `ee.cloud.sessions.schemas` module exposes three schemas: `CreateSessionRequest` for opening a session, `UpdateSessionRequest` for renaming or re-linking it, and `SessionResponse` for API responses.

## Why Schema Tests Matter

Pydantic schemas are the sole contract between the HTTP layer and the service layer. If a default value drifts (e.g., `title` stops defaulting to `"New Chat"`), every client that omits `title` on creation would get a different experience. Similarly, if `deleted_at` suddenly becomes non-nullable in the response schema, serializing a live session would crash.

## CreateSessionRequest

`test_create_session_defaults` verifies the zero-argument constructor produces `title="New Chat"` and `pocket_id=None`. This default matters because the frontend often opens a session before the user types a title, and a sensible default prevents empty-string titles from appearing in the session list.

`test_create_session_all_fields` confirms all four optional-at-creation-time fields (`title`, `pocket_id`, `group_id`, `agent_id`) round-trip correctly. These tests exist because Pydantic's `Optional` and `default=None` behavior can silently break if a field is renamed or its type annotation is widened incorrectly.

## UpdateSessionRequest

All fields on `UpdateSessionRequest` are optional — the update is a partial PATCH. `test_update_session_all_optional` validates that a no-argument constructor produces all `None` fields, ensuring the service layer can detect "nothing to change" vs. an explicit null. `test_update_session_partial` and `test_update_session_pocket_link` verify that setting one field leaves others `None`, preventing accidental field coupling.

## SessionResponse

`SessionResponse` is the read model. `test_session_response` constructs a minimal live session and asserts `deleted_at is None` — confirming the field exists and defaults to `None` rather than being absent. `test_session_response_with_deleted_at` passes an explicit `deleted_at` value, covering the soft-delete path used when a session is archived. `test_session_response_with_pocket` validates the full linked-session shape (`pocket`, `group`, `agent`, `message_count`).

## Soft Deletion

The `deleted_at` field in `SessionResponse` is central to PocketPaw's soft-delete approach: sessions are never hard-deleted from the database; instead, `deleted_at` is set and the API hides them from normal listings. The test ensures the schema supports this without erroring on non-deleted sessions (where `deleted_at` must be `None`, not absent).

## Known Gaps

There are no validation tests for field length limits (e.g., maximum title length) or invalid types (e.g., passing an integer as `pocket_id`). Pydantic may accept these silently unless validators are explicitly defined and tested.
