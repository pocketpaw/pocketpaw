---
{
  "title": "Message Bus Event Types: Channel, InboundMessage, OutboundMessage, SystemEvent",
  "summary": "events.py defines the four core data types that flow through PocketPaw's message bus: the Channel enum identifying communication platforms, InboundMessage for received messages, OutboundMessage for agent replies, and SystemEvent for internal runtime signals.",
  "concepts": [
    "Channel",
    "StrEnum",
    "InboundMessage",
    "OutboundMessage",
    "SystemEvent",
    "session_key",
    "frozen dataclass",
    "is_stream_chunk",
    "is_stream_end",
    "with_content",
    "metadata",
    "event_type"
  ],
  "categories": [
    "bus",
    "events",
    "data-model",
    "messaging"
  ],
  "source_docs": [
    "09134ed8bf672431"
  ],
  "backlinks": null,
  "word_count": 539,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

events.py is the schema layer of PocketPaw's message bus. Every piece of data flowing between channel adapters, the agent loop, and the dashboard passes through one of the types defined here. Keeping these types in a dedicated module — imported by adapters, the bus queue, and the agent loop alike — ensures consistent field names and prevents coupling between layers.

## Channel Enum

`Channel` is a `StrEnum`, meaning each member's value is its string representation (`Channel.TELEGRAM == "telegram"`). This design allows channel values to be stored in databases or JSON without a separate serialization step. The full set covers:

- **Messaging platforms**: TELEGRAM, WHATSAPP, SIGNAL, DISCORD, SLACK, MATRIX, TEAMS, GOOGLE_CHAT
- **Web/API channels**: WEBSOCKET, WEBHOOK, A2A (agent-to-agent)
- **Internal channels**: CLI, SYSTEM (subagents, intentions), ENTERPRISE

New channels are added here first; the rest of the system (adapters, command handlers, formatters) operates on the enum value.

## InboundMessage

`InboundMessage` is a frozen (immutable) dataclass. Immutability is a deliberate choice: messages originate from untrusted external sources and should not be modified in-place as they pass through middleware layers (filters, PII scrubbers, intent classifiers). If a layer needs to alter content, `with_content()` creates a shallow copy with only `content` replaced, leaving all other fields identical.

Key fields:
- `channel` — identifies which adapter produced this message
- `sender_id` — platform-specific user identifier (Telegram user ID, WhatsApp phone number, Slack user ID, etc.)
- `chat_id` — identifies the conversation or chat room; combined with `channel`, this produces the `session_key`
- `content` — the text body of the message
- `timestamp` — defaults to `datetime.now()` at creation time
- `media` — local file paths of any downloaded attachments
- `metadata` — adapter-specific extras (e.g., `thread_ts` for Slack, `message_id` for WhatsApp, `username` for Telegram)

The `session_key` property computes `"{channel}:{chat_id}"` — the canonical identifier used by memory stores, the command handler, and session management to namespace all per-user data.

## OutboundMessage

`OutboundMessage` is a mutable dataclass representing a message the agent wants to send. It mirrors `InboundMessage` in structure but adds streaming support:

- `is_stream_chunk: bool` — true for intermediate tokens from a streaming LLM response
- `is_stream_end: bool` — true for the final sentinel; adapters flush buffers and close indicators on receipt

Not using a frozen dataclass here is intentional: the agent loop may annotate outbound messages with metadata (e.g., attaching media file paths generated during the response) before the message reaches the adapter.

## SystemEvent

`SystemEvent` carries internal runtime signals that are not user-facing messages. `event_type` values include `"tool_start"`, `"tool_end"`, `"error"`, `"agent_start"`, `"agent_end"`. The `data` dict is type-free and event-specific — consumers use `.get()` with fallbacks rather than a strict schema.

SystemEvents are published by the agent loop and consumed by the WebSocket adapter (for dashboard thinking indicators) and any other subscribers registered via `bus.subscribe_system()`.

## Design Rationale

Using plain dataclasses rather than Pydantic models keeps the bus layer dependency-free — channel adapters import only `pocketpaw.bus.events` without pulling in the full validation stack. Pydantic is used at the API boundary (FastAPI request/response models), not in the internal message pipeline.

## Known Gaps

- `metadata` on both `InboundMessage` and `OutboundMessage` is typed as `dict[str, Any]` — there is no per-channel schema for metadata contents. Consumers must defensively use `.get()` rather than attribute access.