---
{
  "title": "End-to-End API Test Suite: Auth, Workspaces, Chat, Pockets, Sessions, and Agents",
  "summary": "A comprehensive end-to-end test suite that exercises the full PocketPaw cloud API surface through HTTP calls against a live FastAPI application. It covers user registration and JWT auth, workspace CRUD and invite flows, group chat with reactions/pins/search, pocket management with widgets and share links, session lifecycle tracking, and agent configuration.",
  "concepts": [
    "JWT auth",
    "workspace CRUD",
    "invite lifecycle",
    "group chat",
    "cursor pagination",
    "soft delete",
    "share links",
    "widgets",
    "ripple spec",
    "sessions",
    "FastAPI",
    "httpx"
  ],
  "categories": [
    "api",
    "testing",
    "auth",
    "chat",
    "pockets",
    "test"
  ],
  "source_docs": [
    "4b958533e382cb52"
  ],
  "backlinks": null,
  "word_count": 594,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Scope and Intent

This is the broadest test file in the cloud test suite. It exercises the entire vertical stack — HTTP request → FastAPI router → service layer → database — across all major product features. Each test class maps to a product surface and verifies that the feature works end-to-end, not just in isolation.

The file uses `pytest-asyncio` with a shared `http` fixture that provides an authenticated `httpx.AsyncClient`. Setup within each test class is handled by an async `_setup()` method that creates prerequisite resources (workspace, group, etc.) before the assertions begin.

## TestAuthFlow

Covers the full authentication lifecycle:

- **Registration** — `POST /auth/register` returns 201 with user data; duplicate email returns 400.
- **Login** — `POST /auth/bearer/login` returns an access token; wrong password returns 400.
- **Profile** — `GET /auth/me` returns profile data; unauthenticated call returns 401.
- **JWT continuity** — `test_jwt_token_works_across_requests` issues a token and uses it on a subsequent request, verifying the token is valid across the full HTTP round trip rather than just being returned.

The `test_update_profile_full_name` test catches a common regression where PATCH endpoints accept the request but fail silently to persist.

## TestWorkspaceFlow

Workspaces are the top-level organizational unit. Key tests:

- **Slug uniqueness** — duplicate slug returns 409, preventing accidental namespace collisions.
- **License gate** — `test_workspace_without_license_returns_403` verifies that unlicensed workspaces cannot access protected features, a critical gate for the enterprise billing model.
- **Invite lifecycle** — create invite → validate by token → accept (adds user to workspace) → revoke (token no longer valid). This full flow catches integration gaps between the invite token system and the membership table.

## TestChatFlow

Chat is the primary collaboration surface. Tests cover:

- **Cursor pagination** — `test_list_messages_with_cursor_pagination` verifies that the cursor mechanism works, not just that messages are returned. Broken pagination would cause the frontend to re-render the same page indefinitely.
- **Soft delete** — deleted messages are marked deleted, not physically removed, so audit history is preserved.
- **Reactions** — toggle behavior (add then remove) is verified as a single idempotent flow rather than two separate operations.
- **Pins** — `test_pin_message_and_list_pinned` then `test_unpin_message` verify the full pin lifecycle.
- **Search** — `test_search_messages_by_content` verifies that full-text search returns the correct message.
- **DMs** — direct messages between two users are created and verified as a distinct room type.

## TestPocketsFlow

Pockets are the AI workspace containers. Notable tests:

- **Ripple spec** — `test_create_pocket_with_ripple_spec` verifies that pockets can be created with a Ripple widget specification, confirming the advanced configuration path works end-to-end.
- **Widget lifecycle** — add widget → update config → remove widget, confirming that widget mutations persist correctly.
- **Share links** — generate token → access via token → revoke token → verify revoked token returns error. This is a security-critical flow; a revoked token that still works would be a data exposure bug.

## TestSessionsFlow

Sessions track agent conversation history. Tests cover CRUD and soft delete, mirroring the chat message pattern.

## Design Patterns

The `http` fixture scope is `function` (default), meaning each test gets a fresh authenticated client. This prevents test pollution at the cost of authentication overhead per test. A `module`-scoped fixture would be faster but risks state leakage between tests.

UUID-based names (e.g., workspace slug derived from `uuid4()`) ensure tests do not collide on slug uniqueness constraints when run in parallel.

## Known Gaps

The `_setup()` pattern is repeated across test classes rather than shared via a common fixture hierarchy. This means setup logic can drift between classes. No explicit teardown — tests rely on `tmp_path`-scoped databases being discarded after each run.