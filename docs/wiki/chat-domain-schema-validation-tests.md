---
{
  "title": "Chat Domain Schema Validation Tests",
  "summary": "This module comprehensively tests all Pydantic schemas in the chat domain — group management, message sending, editing, reactions, WebSocket inbound/outbound messages, cursor pagination, and attachment handling — covering field defaults, length constraints, enum validation, and discriminated union types for WebSocket message dispatch.",
  "concepts": [
    "CreateGroupRequest",
    "SendMessageRequest",
    "EditMessageRequest",
    "ReactRequest",
    "WsInbound",
    "WsOutbound",
    "CursorPage",
    "discriminated union",
    "Pydantic",
    "chat schemas",
    "attachments",
    "WebSocket",
    "group management"
  ],
  "categories": [
    "chat",
    "schemas",
    "testing",
    "WebSocket",
    "test"
  ],
  "source_docs": [
    "e9966d9ee158ac78"
  ],
  "backlinks": null,
  "word_count": 440,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/test_chat_schemas.py` module is the most thorough schema test file in the chat subsystem. It covers every Pydantic model used by the group chat API and WebSocket protocol, ensuring that validation constraints are enforced at the boundary and that the discriminated union for WebSocket message routing works correctly.

## Group Management Schemas

### CreateGroupRequest
- Default `type` is `"private"`, `description` defaults to `""`.
- DM groups accept a `type="dm"` with explicit `member_ids`.
- `name` must be non-empty and under a maximum length — both constraints are tested.
- Invalid `type` values (not in the allowed enum) are rejected.

### UpdateGroupRequest
All fields are optional (PATCH semantics). `test_update_group_partial` confirms that providing only one field does not affect others.

### AddGroupMembersRequest / AddGroupAgentRequest
`AddGroupMembersRequest` accepts a list of user IDs. `AddGroupAgentRequest` has defaults for agent integration parameters, with a custom override test.

## Message Schemas

### SendMessageRequest
- `content` is required and has a minimum length of 1 character and a maximum of 10,000 characters. Both limits are tested.
- `reply_to` defaults to `None`, `mentions` to `[]`.
- Attachments are tested via `test_send_message_with_attachments`, confirming the `attachments` field accepts a list.

### EditMessageRequest
Same length constraints as `SendMessageRequest`. Min and max length tests are included.

### ReactRequest
`emoji` must be a non-empty string with a maximum length. Both bounds are tested.

## WebSocket Schemas

### WsInbound (Discriminated Union)
`WsInbound` uses a Pydantic discriminated union on the `type` field to route incoming WebSocket frames to the correct handler:

```python
msg = WsInbound.model_validate({"type": "message.send", "group_id": "g1", "content": "hello"})
assert msg.type == "message.send"
```

The tested types are: `message.send`, `typing.start`, `react`, `presence`. Invalid `type` values raise `PydanticValidationError`. The `test_ws_inbound_all_types` test iterates over all supported types, ensuring the union is exhaustive.

### WsOutbound
`WsOutbound` represents server-to-client WebSocket frames. `test_ws_outbound_defaults` confirms default field values. `test_ws_outbound` confirms the model accepts the expected fields.

## CursorPage

`CursorPage` is the pagination envelope for list endpoints. `test_cursor_page` confirms the model accepts `items`, `nextCursor`, and `hasMore` fields — matching the `CURSOR_PAGE_KEYS` frozenset in the API contract tests.

## Why Comprehensive Schema Tests

The chat domain schemas are shared between the HTTP API and the WebSocket protocol. A single field type change (e.g., `mentions` becoming a non-optional field) would break both paths simultaneously. Having isolated schema tests means the breakage is caught at the lowest level — schema validation — before integration tests even run.

## Known Gaps

No TODO or FIXME markers. The `WsInbound` discriminated union tests cover four types; if new message types are added, the exhaustiveness check in `test_ws_inbound_all_types` should catch the omission. Attachment field validation (valid URL format, allowed MIME types) is not tested at the schema level.