---
{
  "title": "Smoke Test: Pocket Sidebar Session Creation and Chat Persistence",
  "summary": "This test mirrors the full flow of PocketPaw's enterprise sidebar: a user creates a Pocket, opens a new session under that Pocket, sends a message, and verifies the message appears in the session history API. It validates that `Session` documents are correctly linked to their parent Pocket in MongoDB.",
  "concepts": [
    "Pocket",
    "pocket session",
    "save_user_message",
    "session creation",
    "PocketChatSidebar",
    "context_type",
    "Session document",
    "pocket-session link",
    "chat history",
    "smoke test"
  ],
  "categories": [
    "testing",
    "session management",
    "pockets",
    "chat"
  ],
  "source_docs": [
    "c07b4f27a5e5514c"
  ],
  "backlinks": null,
  "word_count": 402,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## What This Tests

The PocketPaw enterprise dashboard includes a sidebar where users can create AI chat sessions scoped to a specific "Pocket" (a named AI configuration). This smoke test validates that entire flow end-to-end:

1. Register user and workspace
2. Create a Pocket via `POST /api/v1/pockets`
3. Create a session under that Pocket via `POST /api/v1/pockets/{pocket_id}/sessions`
4. Confirm the `Session` document in MongoDB has `pocket=<pocket_id>`, `context_type="pocket"`, correct `owner`, and correct `workspace`
5. Send a user message using `save_user_message(session_id, ...)`
6. Confirm the history API returns that message

## Why the Pocket-Session Link Matters

The `Session` document links a chat conversation to its parent Pocket. This relationship is used by the UI to group sessions under their Pocket in the sidebar, and by the agent runtime to load the correct Pocket configuration (model, system prompt, tools) when processing messages. A broken link means the agent runs with the wrong configuration, or the session appears orphaned in the UI.

## Mongo Verification Pattern

The test queries the `Session` collection directly after the API call:

```python
rows = await Session.find(Session.sessionId == session_id).to_list()
assert s.pocket == pocket_id
assert s.context_type == "pocket"
assert s.owner == user_id
assert s.workspace == workspace["_id"]
```

If the session is not found, the test falls back to a diagnostic dump of all sessions in the database — printing their `sessionId`, `pocket`, `group`, and `context_type` fields. This makes it easy to diagnose whether the session was created with the wrong ID format or linked to the wrong entity type.

## Chat Persistence via `save_user_message`

Rather than going through the full WebSocket/stream endpoint, the test calls `save_user_message(session_id, content)` directly. This internal function is what `POST /api/v1/chat/stream` ultimately calls. Testing it directly keeps the smoke test fast while still validating the storage path.

The resulting message is then verified both via raw Mongo (`context_type="pocket"`, `session_key == session_id`) and via the history endpoint (`GET /api/v1/sessions/{sid}/history`).

## Error Exit Codes

Each failure point returns a distinct exit code:
- `2` — Pocket creation failed
- `3` — Session creation under pocket failed
- `4` — Session not found in MongoDB after creation

This makes CI log triage fast: the exit code tells you exactly which step broke without reading the full output.

## Known Gaps

The test does not cover:
- Multiple sessions under the same Pocket
- Session listing via `GET /api/v1/pockets/{pocket_id}/sessions`
- Agent response messages (only user messages are tested here)
- Session deletion