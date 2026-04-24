---
{
  "title": "Smoke Test: Session Key Normalization Between Agent Bus and UI Chat History",
  "summary": "This test verifies that the colon-delimited session key written by the agent loop (`websocket:X`) is normalized to the underscore form used by the UI (`websocket_X`), so that chat history returned by `GET /api/v1/sessions/{sessionId}/history` actually contains the messages the agent wrote. Without this normalization, the session history API returns empty results despite messages being stored.",
  "concepts": [
    "session key normalization",
    "websocket session",
    "MemoryManager",
    "add_to_session",
    "chat history",
    "session_key",
    "bus-style key",
    "UI sessionId",
    "MongoDB messages collection",
    "license key",
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
    "c96a7da2dbf3f817"
  ],
  "backlinks": null,
  "word_count": 453,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Problem This Test Solves

PocketPaw has two distinct code paths that reference the same logical session using slightly different key formats:

- The **UI** creates a `Session` document with `sessionId = "websocket_<chat_id>"` (underscore separator)
- The **agent loop**, after receiving a WebSocket message, writes memory entries using `session_key = "websocket:<chat_id>"` (colon separator)

If nothing normalizes these two forms to the same value, the history API query (`find messages where session_key == sessionId`) returns zero results — the messages exist in MongoDB but are indexed under the wrong key. This smoke test was written to catch regressions in that normalization layer.

## Test Flow

The test simulates the full round-trip in five steps:

1. **Register user + workspace**: Sets up auth and an active workspace so API calls succeed.
2. **POST /api/v1/sessions**: The UI creates a session; returns `sessionId = "websocket_<uuid>"`.
3. **Agent loop writes via `MemoryManager`**: Calls `manager.add_to_session("websocket:<uuid>", ...)` — the bus-style key.
4. **Raw Mongo inspection**: Queries the `messages` collection directly. Expects exactly 2 rows keyed by `websocket_<uuid>` and 0 rows keyed by `websocket:<uuid>`. If the normalization is working, all rows land under the UI form.
5. **GET /api/v1/sessions/{sessionId}/history**: Confirms the history API returns the two messages in order.

## License Key Helper

The test generates a self-signed enterprise license key inline:

```python
def _license_key(secret: str = "smoke-secret") -> str:
    payload = {"org": "smoke", "plan": "enterprise", "seats": 100, "exp": ...}
    s = json.dumps(payload)
    sig = hashlib.sha256(f"{secret}:{s}".encode()).hexdigest()
    return base64.b64encode(f"{s}.{sig}".encode()).decode()
```

This pattern appears across all cloud smoke tests. It avoids needing a real license server while still exercising the license-verification middleware that wraps most enterprise routes. The secret is known to both sides (`POCKETPAW_LICENSE_SECRET` env var), so the HMAC-based validation passes.

## Agent Pool Mocking

The test patches `get_agent_pool` with a `MagicMock` whose `start` and `stop` methods are `AsyncMock`. This prevents `mount_cloud` from trying to launch real worker processes during test setup. The memory system under test is entirely separate from the agent execution pool, so this stub is safe and keeps the test focused.

## Throwaway Database Pattern

Every cloud smoke script creates a uniquely named MongoDB database:

```python
db_name = f"smoke_chat_stream_keys_{uuid.uuid4().hex[:8]}"
```

The `finally` block drops it unconditionally. This ensures tests never share state and the local MongoDB instance stays clean across repeated runs.

## What a Failure Looks Like

If normalization breaks, step 4 prints:

```
SMOKE FAILED: session_key normalization not applied
```

and returns exit code `4`. The exact exit codes per step make it easy to identify which assertion failed without reading the full output.

## Known Gaps

The test only covers the `websocket:` prefix variant. Other possible prefixes (e.g., `telegram:`, `discord:`) are not checked here. If normalization is prefix-generic, additional cases would provide broader coverage.