---
{
  "title": "Chat Router — REST Endpoints and WebSocket Handler",
  "summary": "The chat domain's HTTP and WebSocket layer. REST routes under `/chat` are license-gated and cover groups, messages, pins, search, and DMs. The WebSocket endpoint at `/ws/cloud` authenticates via JWT query param and dispatches typed JSON messages for real-time chat.",
  "concepts": [
    "FastAPI router",
    "WebSocket",
    "JWT authentication",
    "license gating",
    "message dispatch",
    "broadcast pattern",
    "typing indicators",
    "read receipts",
    "cursor pagination",
    "group management",
    "DMs"
  ],
  "categories": [
    "chat",
    "REST API",
    "WebSocket",
    "routing"
  ],
  "source_docs": [
    "c9b882b919539dad"
  ],
  "backlinks": null,
  "word_count": 560,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Chat Router — REST Endpoints and WebSocket Handler

`cloud/chat/router.py`

## Purpose

This module is the entry point for all chat traffic — both REST API calls and real-time WebSocket connections. It wires together the schema validation, service layer, and connection manager into a cohesive API surface.

## Architecture

### License Gating

All REST endpoints live on a separate `_licensed` sub-router with `dependencies=[Depends(require_license)]`. This means enterprise license validation happens once at the router level, not per-endpoint. The sub-router uses prefix `/chat`, so all REST routes are under `/chat/groups`, `/chat/messages`, etc.

### REST Endpoints

**Groups** — Full CRUD plus membership management:
- `POST /chat/groups` — Create group (public, private, or DM)
- `GET /chat/groups` — List groups visible to the user
- `GET /chat/groups/{id}` — Get single group
- `PATCH /chat/groups/{id}` — Update group (owner only)
- `POST /chat/groups/{id}/archive` — Archive group
- `POST /chat/groups/{id}/join` / `leave` — Self-service join/leave
- `POST /chat/groups/{id}/members` — Add members (owner only)
- `DELETE /chat/groups/{id}/members/{uid}` — Remove member

**Group Agents** — AI agent management in groups:
- `POST /chat/groups/{id}/agents` — Add agent
- `PATCH /chat/groups/{id}/agents/{aid}` — Update agent config
- `DELETE /chat/groups/{id}/agents/{aid}` — Remove agent

**Messages** — CRUD with reactions and threading:
- `GET /chat/groups/{id}/messages` — Cursor-paginated messages
- `POST /chat/groups/{id}/messages` — Send message
- `PATCH /chat/messages/{id}` — Edit message
- `DELETE /chat/messages/{id}` — Soft-delete
- `POST /chat/messages/{id}/react` — Toggle reaction
- `GET /chat/messages/{id}/thread` — Get thread replies

**Pins & Search:**
- `POST/DELETE /chat/groups/{id}/pin/{mid}` — Pin/unpin
- `GET /chat/groups/{id}/search?q=...` — Regex search

**DMs:**
- `POST /chat/dm/{target_user_id}` — Get or create DM channel

### WebSocket Endpoint

`/ws/cloud?token=<JWT>` authenticates via a JWT query parameter rather than headers, because the WebSocket API in browsers does not support custom headers.

**Authentication flow:**
1. Decode JWT using `AUTH_SECRET` env var
2. Verify `sub` claim exists (user ID)
3. Close with code 4001 if invalid
4. Accept connection and register with `ConnectionManager`

**Message dispatch:**
The `_handle_ws_message` function routes validated `WsInbound` messages to type-specific handlers:
- `message.send` / `edit` / `delete` / `react` — Delegate to `MessageService`, then broadcast to group members
- `typing.start` / `typing.stop` — Manage typing indicators via `ConnectionManager`
- `presence.update` — Placeholder (not yet implemented, see Task 19)
- `read.ack` — Broadcast read receipts to group

**Broadcast pattern:** Each WS handler follows the same pattern:
1. Validate required fields (early return if missing)
2. Call the service layer
3. Load the group to get the member list
4. Broadcast the event to all online group members

The sender receives a confirmation message (e.g., `message.sent`) while other members receive the event (e.g., `message.new`).

## Design Decisions

- **JWT in query param**: Browser WebSocket API limitation — no custom headers.
- **Deferred imports inside WS handlers**: `from beanie import PydanticObjectId` and model imports happen inside each handler function rather than at module level. This avoids circular imports since the router imports from schemas/service, which import from models.
- **Hardcoded AUTH_SECRET default**: `"change-me-in-production-please"` — intentionally insecure default to make development easy while being obviously wrong for production.

## Known Gaps

- `presence.update` WebSocket message type is accepted but does nothing — marked as "Task 19".
- The `finally` block after WebSocket disconnect has a `pass` for grace period handling, also deferred to Task 19.
- No rate limiting on WebSocket messages — a client could flood the server.
- JWT audience is hardcoded to `"fastapi-users:auth"` — coupled to the auth provider.
