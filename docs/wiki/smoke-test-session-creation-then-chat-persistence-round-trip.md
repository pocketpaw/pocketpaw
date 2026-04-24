---
{
  "title": "Smoke Test: Session Creation Then Chat Persistence Round-Trip",
  "summary": "This test validates the core \"desired flow\": create a session via the API, send a user message, confirm it is stored in MongoDB keyed by the session ID, and retrieve it through the history endpoint. It was written to capture the exact behavior contract for session-scoped message storage.",
  "concepts": [
    "session creation",
    "save_user_message",
    "chat persistence",
    "session_key",
    "context_type",
    "Message document",
    "history API",
    "pocket context",
    "ASGITransport",
    "smoke test"
  ],
  "categories": [
    "testing",
    "session management",
    "memory",
    "chat"
  ],
  "source_docs": [
    "cc1e2cd3fa4cc9e2"
  ],
  "backlinks": null,
  "word_count": 398,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Intent

The module docstring labels this as testing the flow "we WANT" — meaning it codifies the intended behavior after a bug was fixed, not just a pre-existing path. The bug likely involved messages being stored under the wrong key or with the wrong `context_type`, causing the history API to return empty results.

## The Four-Step Contract

1. `POST /api/v1/sessions` returns `{sessionId: "websocket_<uuid>"}`
2. `save_user_message(sessionId, "hi")` writes a `Message` doc with `context_type="pocket"` and `session_key=sessionId`
3. Raw Mongo confirms rows exist with exactly those fields
4. `GET /api/v1/sessions/{sessionId}/history` returns the message

This contract is simple but the exact field values matter. `context_type="pocket"` means this is a 1:1 AI session (not a group chat or DM). `session_key=sessionId` means the ID used in the history query exactly matches the stored key — no normalization needed because the session was created through the API (as opposed to being opened via WebSocket, which produces a differently formatted key).

## Diagnostic Fallback

If step 3 finds zero rows by `session_key=sessionId`, the test runs a diagnostic dump before failing:

```python
all_msgs = await Message.find().to_list()
for m in all_msgs[:5]:
    print(f"context_type={m.context_type!r} group={m.group!r} session_key={m.session_key!r}")
print("\nSMOKE FAILED: messages did not land keyed by session_id")
return 1
```

This pattern — store what you find even on failure — makes the test self-documenting during a debugging session. You can see what key the message actually landed under and immediately understand the mismatch.

## App Scaffolding Pattern

Like other cloud smoke tests, this script builds a real FastAPI app with `mount_cloud`, initializes Beanie, registers the MongoDB backend, and patches out the agent pool:

```python
with patch("pocketpaw.agents.pool.get_agent_pool", return_value=mock_pool):
    mount_cloud(app)
```

The `ASGITransport` from `httpx` allows HTTP calls against this in-process app without a network. This approach tests real routing, middleware, authentication, and persistence without the overhead of a server process.

## Message Field Assertions

Every message row is validated with explicit field checks:

```python
assert r.context_type == "pocket"
assert r.session_key == session_id
assert r.role in ("user", "assistant", "system")
assert not r.group
```

The `not r.group` assertion prevents pocket messages from being accidentally linked to a chat group, which would make them visible in group chat histories — a significant data isolation bug.

## Known Gaps

The test covers user messages but not assistant responses. Verifying the full round-trip — user message in, agent response out, both in history — would require either a live agent or a mocked agent response path.