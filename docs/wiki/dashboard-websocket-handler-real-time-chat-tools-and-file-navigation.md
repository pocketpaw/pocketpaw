---
{
  "title": "Dashboard WebSocket Handler: Real-Time Chat, Tools, and File Navigation",
  "summary": "dashboard_ws.py implements the core WebSocket handler that drives PocketPaw's real-time dashboard UI — handling authentication, message dispatch, tool execution, session switching, file browsing, and skill invocation over a persistent WebSocket connection.",
  "concepts": [
    "WebSocket handler",
    "authentication",
    "session switching",
    "path traversal prevention",
    "tool execution",
    "file browser",
    "rate limiting",
    "SkillExecutor",
    "dependency injection",
    "real-time communication"
  ],
  "categories": [
    "Dashboard",
    "WebSocket"
  ],
  "source_docs": [
    "56249a7edb5540d9"
  ],
  "backlinks": null,
  "word_count": 428,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/dashboard_ws.py` contains `websocket_handler()`, the central function that manages a WebSocket client's entire session lifecycle. From connection to disconnect, all real-time interactions flow through this handler: chat messages, tool calls, file navigation, session management, API key updates, and daemon commands.

## Connection Lifecycle

When a client connects, the handler:

1. **Rate-limits** the connection by IP using `ws_limiter`. Connections exceeding the limit are closed with code 4029 before any handshake completes.
2. **Authenticates** via the first message (`{"type": "auth", "token": "..."}`) or via a cookie present in the WebSocket upgrade headers. The handler supports three token formats: master access token, derived session token (`expires:hmac`), and API-level tokens.
3. **Registers** the connection with `ws_adapter` and `active_connections`.
4. **Enters the message loop** to process incoming frames until `WebSocketDisconnect`.

## Dependency Injection for Testability

`websocket_handler()` accepts `_is_genuine_localhost_fn` and `_get_access_token_fn` as optional keyword arguments. These are injected by `dashboard.py` at call time from the auth module. This avoids a circular import between `dashboard_ws.py` and `dashboard_auth.py`, while also allowing tests to inject mocks without monkeypatching module globals.

## Session Switching

When the client sends `{"type": "switch_session", "session_id": "..."}`, the handler validates the session ID against allowed sessions for the current user, then loads the session history from memory. A critical fix (2026-03-08): previously, a switch to a non-existent or path-traversal session ID would silently hang — the handler would neither send a response nor raise an error, leaving the client waiting indefinitely. The fix sends an empty `session_history` response so the client knows the switch "succeeded" to an empty session.

## Path Traversal Defense in File Browse

`handle_file_browse()` resolves the requested path and checks that it doesn't escape the user's home directory or allowed roots. Requests for paths like `../../etc/passwd` are rejected with an error response before any directory listing occurs.

## Tool Execution

`handle_tool()` dispatches tool calls to `SkillExecutor`. Tools receive the current `settings` object and the full message `data` dict. Results are streamed back as `tool_result` frames.

## API Key Handling

When the client sends `{"type": "save_api_key", ...}`, the handler validates the key against the configured backend, saves it to settings, and returns a `api_key_saved` response. The `_api_key_response()` helper builds this response, optionally including `warnings` (e.g., "key accepted but rate limits apply").

## Known Gaps

- `handle_file_navigation()` and `handle_file_browse()` are exposed as module-level functions but are tightly coupled to the WebSocket protocol. They cannot be easily tested without a real WebSocket connection.
- The session switch path-traversal fix (2026-03-08) sends an empty history response rather than an explicit error. A future improvement would distinguish "session not found" from "session is empty".