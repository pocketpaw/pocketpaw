---
{
  "title": "Message Bus: Central Routing Hub for All Channel Communication",
  "summary": "MessageBus is the central publish-subscribe hub in PocketPaw that decouples channel adapters from the agent loop. Adapters publish inbound messages to a bounded asyncio queue; the agent loop consumes them. Outbound messages are fanned out to registered adapter callbacks. System events flow separately to dashboard and monitoring subscribers.",
  "concepts": [
    "MessageBus",
    "asyncio.Queue",
    "publish_inbound",
    "consume_inbound",
    "subscribe_outbound",
    "publish_outbound",
    "broadcast_outbound",
    "system events",
    "backpressure",
    "fan-out",
    "asyncio.gather",
    "singleton",
    "return_exceptions"
  ],
  "categories": [
    "bus",
    "messaging",
    "pub-sub",
    "architecture"
  ],
  "source_docs": [
    "9777fd982ba4d6a3"
  ],
  "backlinks": null,
  "word_count": 520,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

MessageBus implements the core routing logic that makes PocketPaw channel-agnostic. Channel adapters, the agent loop, and the dashboard are all connected through this single object — none of them hold direct references to each other. Adding a new channel requires only creating an adapter that publishes to and subscribes from the bus; no other component needs to change.

## Inbound Path (Channel → Agent)

`publish_inbound()` places an `InboundMessage` on an `asyncio.Queue` with a default max size of 1,000. The queue provides backpressure: if the agent loop falls behind, `put()` will block (await) until space is available, preventing unbounded memory growth from message bursts.

The agent loop calls `consume_inbound()` in a tight loop with a 1-second timeout. The timeout allows the loop to perform periodic housekeeping (checking for shutdown signals, flushing metrics) without blocking indefinitely when no messages arrive.

`inbound_pending()` exposes `_inbound.qsize()` for monitoring and health checks.

## Outbound Path (Agent → Channel)

Each channel adapter registers its `send()` method via `subscribe_outbound(channel, callback)`. Multiple adapters can subscribe to the same channel (e.g., two Telegram bots with different tokens), and all will receive the message.

`publish_outbound()` fans the message to all subscribers for the message's channel using `asyncio.gather(*tasks, return_exceptions=True)`. The `return_exceptions=True` flag is critical: it prevents one failing subscriber from cancelling delivery to others. Failed subscriber calls are logged individually without raising.

`unsubscribe_outbound()` uses a `try/except ValueError` around `list.remove()` rather than a guard check — safe for the remove-if-present pattern and avoids a TOCTOU race in concurrent code.

`broadcast_outbound()` sends a message to all subscribed channels except an optional `exclude` channel. It reconstructs an `OutboundMessage` with the correct `channel` for each subscriber rather than reusing the original — this ensures the adapter receives a message with its own `Channel` value, preventing channel mismatch errors.

## System Events Path

System events (`tool_start`, `tool_end`, `agent_start`, etc.) flow on a separate list of subscribers, not through the queue. This keeps them out of the inbound backpressure path and ensures they are delivered synchronously to all listeners in the order published. The WebSocket adapter subscribes here to push thinking indicators to the dashboard in real time.

## Lifecycle and Singleton

`get_message_bus()` returns a module-level singleton. On first call, it also registers a reset function with PocketPaw's lifecycle manager so that `_bus = None` is called on application shutdown. The next call to `get_message_bus()` after a shutdown/restart will create a fresh bus with empty queues and no subscribers — important for test isolation.

`clear()` drains the inbound queue without cancelling subscribers. It is primarily used in tests to reset state between test cases.

## Design Principles

The bus's docstring enumerates its four design principles:
1. Single source of truth for message flow
2. Decouples channels from agent logic
3. Supports multiple subscribers per channel
4. Async-first with proper backpressure

## Known Gaps

- There is no dead-letter queue for messages that fail delivery to all subscribers. A message with no subscribers logs a warning and is silently dropped.
- The inbound queue is in-memory only. A process crash loses all queued but unprocessed messages. Persistent queueing (Redis, etc.) would be needed for production reliability guarantees.