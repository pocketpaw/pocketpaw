---
{
  "title": "Chat Schemas - Request, Response, and WebSocket Message Models",
  "summary": "This module defines all Pydantic models used by the chat domain: REST request bodies, response shapes, cursor pagination, and WebSocket message envelopes. Centralising schemas here decouples validation logic from both the router and service layers.",
  "concepts": [
    "Pydantic",
    "request schemas",
    "response schemas",
    "cursor pagination",
    "WebSocket schemas",
    "group types",
    "member roles",
    "agent respond_mode",
    "partial updates",
    "WsInbound",
    "WsOutbound",
    "literal types"
  ],
  "categories": [
    "chat",
    "cloud EE",
    "schemas",
    "API"
  ],
  "source_docs": [
    "94fa2fb94ade558c"
  ],
  "backlinks": null,
  "word_count": 382,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`schemas.py` is the single source of truth for all data shapes exchanged in the chat domain. Having one file for schemas prevents divergence when routers and services define their own ad-hoc dicts - a change to the message shape is made once and propagates everywhere.

## REST Request Schemas

### `CreateGroupRequest`

The `min_length=1` constraint on `name` prevents empty-string groups from being created at the API boundary. The `type` literal (`public`, `private`, `dm`, `channel`) constrains the group kind so the service layer does not need to validate it again.

### `UpdateGroupRequest`

All fields are optional (`str | None = None`), enabling partial PATCH semantics. Clients send only the fields they wish to change. The `type` field on updates excludes `"dm"` since DMs cannot be retyped after creation.

### `AddGroupMembersRequest` / `UpdateMemberRoleRequest`

Member management schemas keep role values as literals (`"edit"`, `"view"`) so only valid roles can be submitted. The service layer maps these literals to the internal `MemberRole` enum.

### Agent Management Schemas

`AddGroupAgentRequest` and `UpdateGroupAgentRequest` control how AI agents are attached to groups. `respond_mode` (defaulting to `"auto"`) determines whether the agent replies to every message or only when mentioned. It is a plain string rather than a literal, allowing future modes without a schema migration.

## REST Response Schemas

`MessageResponse` and `GroupResponse` document the canonical API response shape. They serve as living documentation and can be referenced in OpenAPI generation.

### `CursorPage`

Cursor pagination avoids the offset drift problem (items shifting as new messages are inserted) that affects offset-based pagination. The cursor is an opaque string - currently an ObjectId - allowing the backend to change the cursor implementation without a client-facing API change.

## WebSocket Schemas

### `WsInbound`

Validated inbound WebSocket message from the client. Wrapping WS messages in a Pydantic model ensures malformed messages (missing action, wrong field types) are rejected before reaching the handler, rather than causing obscure `KeyError` exceptions mid-dispatch.

### `WsOutbound`

The server-to-client envelope. All server pushes use this shape, providing a consistent `type` discriminator that the client can switch on.

## Known Gaps

- `MessageResponse` and `GroupResponse` are not consistently used as FastAPI `response_model` annotations; OpenAPI docs may diverge from actual responses.
- `AddGroupAgentRequest.respond_mode` accepts any string; invalid modes are only caught at runtime in the service layer rather than at the API boundary.