---
{
  "title": "Chat Router - REST Endpoints and WebSocket Handler",
  "summary": "The chat router exposes the full `/chat` REST surface and the `/ws/cloud` WebSocket endpoint, both gated behind an enterprise license check. It manages presence lifecycle with a 30-second grace period and dispatches inbound WebSocket messages to the appropriate service methods.",
  "concepts": [
    "chat router",
    "WebSocket",
    "presence",
    "PresenceOnline",
    "PresenceOffline",
    "enterprise license",
    "JWT authentication",
    "REST endpoints",
    "RBAC",
    "message dispatch",
    "typing indicators",
    "workspace search"
  ],
  "categories": [
    "chat",
    "cloud EE",
    "WebSocket",
    "realtime"
  ],
  "source_docs": [
    "1794aae01377c778"
  ],
  "backlinks": null,
  "word_count": 378,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`router.py` is the HTTP and WebSocket entry point for the chat domain. All REST routes are prefixed under `/chat` and require an active enterprise license. The WebSocket endpoint at `/ws/cloud` authenticates via a JWT passed as a query parameter because the browser WebSocket API does not support custom headers.

## License Gating

Every REST endpoint includes a license dependency, so requests from workspaces without an enterprise license receive a 403 before any business logic runs. The dependency is applied at router level so individual endpoints do not need to repeat it.

## Presence Lifecycle

Presence management was added in Task 19, Cluster A sub-PR 4. Two events drive the online indicator:

- **`PresenceOnline`** - fired immediately when the first socket for a user is accepted. If a user has two tabs open, closing one must not trigger offline.
- **`PresenceOffline`** - fired after a 30-second grace period following the user's last socket disconnecting. This prevents the online indicator from flapping during page reloads.

On connect, the server also sends the new socket a snapshot of currently-online workspace peers. Without this, a user joining an active workspace would see everyone as offline until the next presence delta arrived.

## WebSocket Message Dispatch

Inbound WebSocket messages are validated against `WsInbound` and routed to typed handler functions by action type:

| Action | Handler |
|--------|--------|
| `send` | `_ws_message_send` |
| `edit` | `_ws_message_edit` |
| `delete` | `_ws_message_delete` |
| `react` | `_ws_message_react` |
| `typing.start` / `typing.stop` | `_ws_typing` |
| `read.ack` | `_ws_read_ack` |

Dispatch is centralised in `_handle_ws_message`, which keeps error handling (malformed messages, missing group IDs) in one place.

## Workspace-Wide Message Search

`GET /chat/messages/search` was added in Cluster E sub-PR 2. It delegates to `MessageService.search_workspace_messages`, which scopes results to groups the authenticated user belongs to. No additional access-control logic is needed at the router level because the scope filter lives in the service.

## RBAC Integration

REST endpoints that mutate data pass through `pocketpaw.ee.guards.rbac` dependencies before reaching the service, ensuring the RBAC matrix is enforced consistently.

## Known Gaps

- JWT-in-query-param means tokens can appear in server access logs. A short-lived one-time token exchange would mitigate log exposure.
- The 30-second presence grace period is a hardcoded constant, not configurable per workspace.