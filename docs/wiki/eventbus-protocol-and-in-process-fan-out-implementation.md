---
{
  "title": "EventBus Protocol and In-Process Fan-Out Implementation",
  "summary": "This module defines the EventBus protocol and the InProcessBus implementation that fans out realtime events to WebSocket connections on the same process, routing through AudienceResolver to determine recipients and ConnectionManager to deliver payloads. Module-level singletons with explicit initialisation guards make it safe to wire up during app startup while crashing loudly if accessed before initialisation.",
  "concepts": [
    "EventBus",
    "Protocol",
    "InProcessBus",
    "publish",
    "AudienceResolver",
    "ConnectionManager",
    "WsOutbound",
    "singleton initialisation",
    "lazy import",
    "fan-out",
    "RedisBus",
    "module-level state"
  ],
  "categories": [
    "realtime",
    "event bus",
    "WebSocket",
    "EE cloud"
  ],
  "source_docs": [
    "947b2382255a19b2"
  ],
  "backlinks": null,
  "word_count": 465,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/realtime/bus.py` is the core dispatch layer of the realtime system. It separates the "what to send" concern (handled by `AudienceResolver` in `audience.py`) from the "how to send it" concern (handled by `ConnectionManager`), acting as the glue between them.

## EventBus Protocol

```python
class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...
```

The `Protocol` class makes `EventBus` structurally typed — any object with an `async publish(event)` method satisfies it, without needing to inherit from `EventBus`. This enables a future `RedisBus` implementation to work with all existing call sites by simply implementing `publish`, with no change to service code or the `emit.py` facade.

## InProcessBus

The `InProcessBus.publish` method performs three steps: resolve the audience, construct the `WsOutbound` payload, and deliver to each user via `ConnectionManager.send_to_user`.

The `WsOutbound` import is lazy (inside the `publish` method body) rather than at module top. This is explicitly documented as a solution to a real problem: during pytest collection, loading `ee.cloud.chat.schemas` before the realtime module was fully initialised caused `ImportError` under specific test ordering. The lazy import breaks the cycle by deferring the import until `publish` is actually called, by which point all modules are fully loaded.

Error handling in `publish` is split into two layers. Audience resolution failures are caught and logged at `exception` level (with full traceback), then the method returns early. Individual send failures per user are caught and logged at `warning` level with `exc_info=True` but processing continues to the next user. This means a broken connection for one user does not prevent other users from receiving the event.

```python
async def publish(self, event: Event) -> None:
    try:
        audience = await self._resolver.audience(event)
    except Exception:
        logger.exception("audience resolution failed for event %s", event.type)
        return
    payload = WsOutbound(type=event.type, data=event.data)
    for uid in audience:
        try:
            await self._conn.send_to_user(uid, payload)
        except Exception:
            logger.warning("ws send failed; user=%s event=%s", uid, event.type, exc_info=True)
```

## Module-Level Singletons

The bus and resolver are stored as module-level variables (`_bus`, `_resolver`) and accessed via `get_bus()` / `get_resolver()`. These functions use `assert` to fail loudly if called before `set_bus` / `set_resolver` — this surfaces programmer errors (forgetting to call `init_realtime` at startup) as `AssertionError` immediately in tests rather than as a confusing `AttributeError` at the point of first use.

The `set_bus` / `set_resolver` functions are also useful in tests: they allow a test to install a mock bus without patching module internals.

## Known Gaps

- **RedisBus not implemented**: Task 33 tracks the implementation of a Redis Pub/Sub-backed bus for multi-process deployments. Until then, events cannot cross process boundaries.
- **No dead-letter queue**: Events that fail audience resolution or delivery are logged and dropped. There is no retry or dead-letter mechanism.
- **Single ConnectionManager**: The `InProcessBus` holds a reference to one `ConnectionManager`. In a future multi-tenant or multi-channel setup, routing to the correct manager would require additional logic.