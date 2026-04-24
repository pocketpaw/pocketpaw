---
{
  "title": "Internal Async Event Bus: Subscribe, Emit, and Fault Isolation Tests",
  "summary": "This test file validates the `ee.cloud.shared.events` module's async event bus, which enables decoupled communication between cloud subsystems without direct imports. The tests confirm subscription, emission, unsubscription, multi-handler ordering, handler fault isolation, and the existence of a module-level singleton.",
  "concepts": [
    "event bus",
    "observer pattern",
    "async handlers",
    "subscribe",
    "emit",
    "unsubscribe",
    "fault isolation",
    "module singleton",
    "decoupled communication",
    "EventBus"
  ],
  "categories": [
    "testing",
    "event-driven architecture",
    "async",
    "test"
  ],
  "source_docs": [
    "d826ce84999ba180"
  ],
  "backlinks": null,
  "word_count": 497,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's cloud layer uses an internal async event bus to decouple subsystems. Instead of one module directly calling another, it emits a typed event (e.g., `user.created`, `invite.accepted`) and any interested handler subscribes. This is the classic observer pattern applied to an async Python runtime.

## Why This Exists

Without an event bus, adding a new side effect to an existing operation requires editing the operation's code directly — creating tight coupling. For example, sending a welcome notification when a user registers would require the auth module to import the notification module. With the event bus, the notification module subscribes to `user.created` independently. The auth module never knows the notification module exists.

## Test Cases and Their Motivations

**`test_subscribe_and_emit`** — The basic contract: a subscribed handler receives exactly the data payload emitted. This test creates a fresh `EventBus` instance rather than using the module singleton to avoid inter-test pollution.

**`test_multiple_handlers`** — Two handlers on the same event must both be called, in subscription order. This matters for predictable side effects: if the notification handler fires before the analytics handler, that ordering should be stable and not depend on dict iteration order.

**`test_emit_unknown_event_does_nothing`** — Emitting an event with no subscribers must not raise. This is the "fire and forget" guarantee — callers emit without knowing whether anyone is listening. Requiring at least one subscriber would break the decoupling goal.

**`test_unsubscribe`** — A handler removed via `unsubscribe` must not be called on subsequent emits. This prevents resource leaks: if a component is torn down (e.g., a pocket is deleted), its handlers should be cleaned up so they don't accumulate in memory.

**`test_handler_error_does_not_stop_others`** — If one handler raises, the remaining handlers on the same event must still execute. This is the critical fault isolation guarantee. Without it, a buggy analytics handler would silently prevent notification delivery to the user. The event bus logs or suppresses the error and continues.

**`test_module_level_singleton`** — The `event_bus` exported from `ee.cloud.shared.events` is an `EventBus` instance. This confirms the module wires up a shared singleton that all subsystems can import and use without manual instantiation.

## Architecture Implications

The bus is async-native: handlers are `async def` coroutines and are `await`ed during `emit`. This means `emit` itself is async and should be called with `await`. Sync handlers are not supported — this is intentional to prevent blocking the event loop in a FastAPI async context.

```python
bus = EventBus()

async def handler(data: dict) -> None:
    await send_notification(data["user_id"])

bus.subscribe("user.created", handler)
await bus.emit("user.created", {"user_id": "u1"})
```

## Known Gaps

The test suite does not cover:
- **Concurrent emits** — What happens if two coroutines emit the same event simultaneously. The current implementation likely uses a simple list iteration, which is not thread-safe under `asyncio.gather` if handlers mutate the subscriber list.
- **Wildcard subscriptions** — There is no evidence of glob-style event matching (e.g., `user.*`). All subscriptions are exact string matches.
- **Persistence** — The bus is purely in-process and in-memory. Events are not persisted or replayed on restart.
