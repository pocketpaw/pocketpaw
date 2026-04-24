---
{
  "title": "Message Bus Package Public API",
  "summary": "The pocketpaw.bus package re-exports the full public surface of the messaging subsystem: event types (InboundMessage, OutboundMessage, SystemEvent, Channel), the MessageBus and its singleton accessor, and the channel adapter contracts (ChannelAdapter protocol and BaseChannelAdapter base class). All runtime code that sends or receives messages imports from here rather than from sub-modules.",
  "concepts": [
    "MessageBus",
    "get_message_bus",
    "InboundMessage",
    "OutboundMessage",
    "SystemEvent",
    "Channel",
    "ChannelAdapter",
    "BaseChannelAdapter",
    "event-driven architecture",
    "pub-sub",
    "channel routing"
  ],
  "categories": [
    "message-bus",
    "event-system",
    "package-structure"
  ],
  "source_docs": [
    "0000000000000010"
  ],
  "backlinks": null,
  "word_count": 455,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Role of the Message Bus

PocketPaw's agent runtime is channel-agnostic: the same `AgentLoop` handles messages from Telegram, Discord, Matrix, Google Chat, and any future adapter. The `MessageBus` is the central async queue that decouples channel adapters (which receive and send raw platform messages) from the agent loop (which processes them as structured events). This package's `__init__.py` defines the stable public surface that both sides program against.

## Exported Symbols

### Event Types
- **`InboundMessage`** — A normalised representation of a message received from any channel. Contains the sender, channel, content, and metadata in a uniform structure regardless of the source platform.
- **`OutboundMessage`** — A message to be sent to a specific channel and recipient. Carries the content, target channel enum, and the `chat_id` needed for delivery.
- **`SystemEvent`** — Internal bus events (adapter connected, adapter disconnected, health change, etc.) that the agent loop and other subscribers can react to without coupling to specific adapters.
- **`Channel`** — An enum of all supported channels (`TELEGRAM`, `DISCORD`, `MATRIX`, `GOOGLE_CHAT`, `WEBSOCKET`, etc.). Used as a routing key so the bus can deliver outbound messages to the correct adapter.

### Bus
- **`MessageBus`** — The async queue at the centre of the system. Adapters publish `InboundMessage` and `SystemEvent` instances; the agent loop subscribes and processes them. The agent loop publishes `OutboundMessage` instances; adapters subscribe and dispatch them to the platform.
- **`get_message_bus()`** — Returns the process-level singleton `MessageBus`. All components call this function to obtain the shared bus instance, avoiding the need to thread a bus reference through every constructor.

### Adapter Contracts
- **`ChannelAdapter`** — A `Protocol` defining the three methods every adapter must implement: `channel()` (returns the `Channel` enum value), `start(bus)`, and `stop()`.
- **`BaseChannelAdapter`** — An abstract base class providing the `start()`/`stop()` lifecycle wrapper and the `send()` helper. Concrete adapters extend this and implement `_on_start()`, `_on_stop()`, and `send()`.

## Design Principle

By re-exporting everything from a single `__init__.py`, the bus subsystem presents a flat API. Adding a new event type or changing where `MessageBus` lives internally does not require updating every consumer — only this file needs to change. This is the same pattern used by `pocketpaw.bootstrap`.

## Event Flow

The full message lifecycle moves in two directions. Inbound: a platform sends a message to an adapter, which normalises it to `InboundMessage` and publishes it to the bus. The agent loop subscribes, processes the message through the AI model, and emits an `OutboundMessage`. Outbound: the bus routes the `OutboundMessage` to the adapter registered for that `Channel`, which converts it to the platform's native format and sends it. `SystemEvent` messages flow in both directions and allow the agent loop to react to adapter lifecycle changes without polling.

## Known Gaps

None at the package level.