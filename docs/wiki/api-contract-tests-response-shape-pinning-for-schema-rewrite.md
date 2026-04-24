---
{
  "title": "API Contract Tests: Response Shape Pinning for Schema Rewrite",
  "summary": "This module pins the exact JSON field sets for PocketPaw's cloud messaging and session endpoints before a planned unified-schema rewrite, using frozen `frozenset` constants as shape contracts. Tests run against real MongoDB via `mongomock` and a full ASGI test client, ensuring any field rename or addition detected post-rewrite breaks the test by design.",
  "concepts": [
    "API contracts",
    "response shape",
    "frozenset",
    "schema rewrite",
    "MessageResponse",
    "CursorPage",
    "SessionResponse",
    "cursor pagination",
    "ISO 8601",
    "mongomock",
    "ASGI",
    "httpx",
    "contract testing"
  ],
  "categories": [
    "API",
    "testing",
    "contracts",
    "sessions",
    "chat",
    "test"
  ],
  "source_docs": [
    "5f1cc6baf22f88c6"
  ],
  "backlinks": null,
  "word_count": 422,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/test_api_contracts.py` module serves a specialized purpose: it is a pre-rewrite snapshot suite. Before the unified-schema rewrite (`T2`), this file records the exact JSON key sets returned by five critical endpoints. After the rewrite, these tests must remain green — any deviation indicates an unintended breaking change.

## Frozen Shape Constants

Five `frozenset` constants define the expected key sets:

```python
MESSAGE_RESPONSE_KEYS = frozenset({
    "_id", "group", "sender", "senderType", "agent", "content",
    "mentions", "replyTo", "attachments", "reactions", "edited",
    "editedAt", "deleted", "createdAt"
})
CURSOR_PAGE_KEYS = frozenset({"items", "nextCursor", "hasMore"})
SESSION_RESPONSE_KEYS = frozenset({"_id", "sessionId", "workspace", "owner", ...})
RUNTIME_SESSIONS_ENVELOPE_KEYS = frozenset({...})
HISTORY_ENVELOPE_KEYS = frozenset({...})
```

Using `frozenset` equality for assertion means tests fail on both added and removed fields — the check is bidirectional. This is intentional: even adding a new field (which could break typed clients) should require explicit review.

## Test Infrastructure

### License Key and Auth Context

The `_make_license_key(secret)` helper generates a deterministic HMAC-based license key for test environments. The `auth_ctx` fixture registers a fresh user, creates a workspace, and sets it as active — providing a realistic authentication context for each test class.

### ASGI Client

The `http` fixture uses `httpx.AsyncClient` with `ASGITransport` rather than FastAPI's `TestClient`. This enables async test functions and more accurately simulates real HTTP behavior, including header handling and response streaming.

### Database

The `beanie_db` fixture uses `mongomock_motor` for an isolated in-memory MongoDB instance. Tests are self-contained and do not require a running MongoDB server.

## TestMessageContract

Tests `POST /api/v1/chat/groups/{id}/messages` and `GET /api/v1/chat/groups/{id}/messages`:

- `test_send_message_response_shape` posts a message and asserts `response.json().keys() == MESSAGE_RESPONSE_KEYS`.
- `test_send_message_with_mentions_and_reply_shape` tests that `mentions` and `replyTo` are present even when populated.
- `test_list_messages_cursor_page_shape` verifies the cursor pagination envelope.
- `test_list_messages_empty_page_shape` verifies the empty state returns the same envelope shape (not a bare `[]`).

## TestSessionContract

Tests session listing, runtime session envelope, and history:
- `test_create_and_list_session_shape` creates a session then lists sessions, asserting the response item shape.
- `test_runtime_sessions_envelope_shape` verifies the runtime session wrapper.
- `test_session_history_empty_envelope_shape` confirms history returns a proper envelope even when empty.

The `_assert_iso8601` helper validates that timestamp fields are properly formatted ISO 8601 strings — a common serialization bug where `datetime` objects are rendered as Python `repr` strings instead of JSON strings.

## Known Gaps

The file acknowledges that fixtures are duplicated from `tests/cloud/test_e2e_api.py` to keep it self-contained — this is a maintenance burden if the auth flow changes. The `RUNTIME_SESSIONS_ENVELOPE_KEYS` and `HISTORY_ENVELOPE_KEYS` constants are referenced but not shown in the AST extract, meaning their full set of fields is not visible without reading the source directly. No error-path contract tests are included.