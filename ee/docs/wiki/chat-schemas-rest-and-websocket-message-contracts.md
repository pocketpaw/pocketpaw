---
{
  "title": "Chat Schemas — REST and WebSocket Message Contracts",
  "summary": "Pydantic schemas defining the data contracts for the entire chat domain: group CRUD requests, message operations, cursor-based pagination responses, and typed WebSocket inbound/outbound message formats.",
  "concepts": [
    "Pydantic schemas",
    "CreateGroupRequest",
    "SendMessageRequest",
    "WsInbound",
    "WsOutbound",
    "CursorPage",
    "cursor pagination",
    "MessageResponse",
    "GroupResponse",
    "Literal type"
  ],
  "categories": [
    "chat",
    "schemas",
    "API contracts",
    "WebSocket"
  ],
  "source_docs": [
    "e6dcb59af397e713"
  ],
  "backlinks": null,
  "word_count": 456,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Chat Schemas — REST and WebSocket Message Contracts

`cloud/chat/schemas.py`

## Purpose

This module defines every data shape that crosses the chat domain boundary — inbound requests, outbound responses, and WebSocket wire formats. By centralizing schemas here, the router and service layers share a single source of truth for validation.

## REST Request Schemas

### Group Operations

- **CreateGroupRequest** — Name (1-100 chars), type (`public`/`private`/`dm`), optional member IDs, icon, color. The `min_length=1` on name prevents empty group names that would produce invalid slugs.
- **UpdateGroupRequest** — All optional fields for partial updates.
- **AddGroupMembersRequest** — List of user IDs to add.
- **AddGroupAgentRequest** — Agent ID, role (default `"assistant"`), respond mode (default `"auto"`).
- **UpdateGroupAgentRequest** — Only `respond_mode` is updatable.

### Message Operations

- **SendMessageRequest** — Content (1-10,000 chars), optional reply_to for threading, mentions and attachments as `list[dict]`.
- **EditMessageRequest** — Same content constraints as send.
- **ReactRequest** — Emoji string (1-50 chars, accommodates multi-codepoint emoji).

## REST Response Schemas

### MessageResponse

Flat representation of a message with all metadata: sender info, content, mentions, attachments, reactions, edit/delete status, and timestamps.

### GroupResponse

Complete group state including populated members and agents lists. Uses `list[Any]` for members and agents because these can be either IDs or populated objects depending on context.

### CursorPage

Cursor-based pagination wrapper: `items` (list of MessageResponse), `next_cursor` (opaque string for the next page), `has_more` (boolean). Cursor pagination is chosen over offset pagination because it handles concurrent inserts correctly — new messages don't shift page boundaries.

## WebSocket Schemas

### WsInbound

Union-style message from the client. Uses a `Literal` type field to restrict valid message types:
- `message.send`, `message.edit`, `message.delete`, `message.react`
- `typing.start`, `typing.stop`
- `presence.update`
- `read.ack`

All payload fields are optional since different message types use different subsets. The router validates which fields are required for each type.

### WsOutbound

Simpler structure: a `type` string and a `data` dict. Less strictly typed than inbound because the server sends many different event shapes.

## Design Decisions

- **Flat WsInbound model**: Rather than a discriminated union with separate models per type, all fields live on one model with most being optional. This trades type safety for simplicity — the dispatch layer validates required fields.
- **dict for mentions/attachments**: Using `list[dict]` instead of typed models gives flexibility as these structures evolve, at the cost of less validation.
- **10,000 char message limit**: Prevents extremely large messages from consuming memory/bandwidth while still being generous enough for code blocks and long discussions.

## Known Gaps

- `mentions` and `attachments` are `list[dict]` without typed schemas — invalid structures would pass validation.
- `WsInbound` does not validate field requirements per message type at the schema level — a `message.send` without `content` passes validation and must be caught in the handler.
