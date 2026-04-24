---
{
  "title": "In-Process Async Event Bus for Cloud Domain Communication",
  "summary": "This module implements a lightweight in-process pub/sub event bus that lets cloud domains react to events from other domains without direct imports. It is the backbone for cross-domain side effects in the PocketPaw EE layer, used by the agent bridge, event handlers, and any future domain that needs to observe another.",
  "concepts": [
    "EventBus",
    "pub/sub",
    "in-process messaging",
    "asyncio",
    "event subscription",
    "handler lifecycle",
    "defaultdict",
    "event-driven architecture",
    "cross-domain decoupling",
    "sequential emit"
  ],
  "categories": [
    "event handling",
    "architecture",
    "cloud EE"
  ],
  "source_docs": [
    "2733169e11225009"
  ],
  "backlinks": null,
  "word_count": 456,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/shared/events.py` provides `EventBus`, a simple async pub/sub system scoped to a single process. It exposes a module-level `event_bus` singleton used throughout the cloud EE layer.

## Why In-Process Pub/Sub

A direct function call between domains requires the caller to import the callee — creating a coupling that can spiral into circular imports. A message queue (Kafka, Redis pub/sub) adds operational complexity and latency for side effects that are inherently local to the request. The in-process event bus gives decoupling without infrastructure overhead: domains subscribe once at startup and fire events without knowing who observes them.

## EventBus Design

The bus stores handlers in a `defaultdict(list)` keyed by event name. `subscribe` appends a coroutine handler; `unsubscribe` removes it. `emit` iterates handlers for the event name and awaits each one sequentially:

```python
async def emit(self, event: str, data: dict) -> None:
    for handler in self._handlers[event]:
        try:
            await handler(data)
        except Exception:
            logger.exception("Event handler %s failed for event %s", handler, event)
```

The `try/except` around each handler prevents a single failing subscriber from aborting other subscribers. This is important for the `message.sent` path: if the notification handler crashes, the agent bridge handler should still fire.

## Handler Type Contract

Handlers must have the signature `async (data: dict) -> None`. The `Handler` type alias makes this explicit:

```python
Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
```

Using `dict` as the payload type keeps the bus generic and avoids binding it to specific Pydantic models. Callers extract fields with `.get()` and handle missing keys gracefully.

## Subscription Lifecycle

`unsubscribe` is provided but rarely called in production — handlers are registered at startup and live for the process lifetime. The method exists primarily to support test teardown: a test that subscribes a mock handler can clean it up after the test to prevent interference with subsequent tests.

## Sequential Emit and Its Implications

Handlers for a given event are awaited one at a time. This means a slow handler (e.g., one that hits MongoDB) adds latency to every subsequent handler for the same event. The agent bridge sidesteps this by immediately dispatching to a background `asyncio.Task` inside its handler, keeping the emit cycle fast regardless of how long the actual agent response takes.

## Known Gaps

- There is no support for wildcard subscriptions (e.g., subscribing to `"message.*"` to catch all message events). Each event name must be subscribed individually.
- The bus holds strong references to all handler callables. If a handler is a method on an object, the bus will prevent that object from being garbage-collected until the handler is explicitly unsubscribed.
- There is no observability hook (metrics, tracing) on the emit path. Tracking which events fire most frequently, or which handlers are slowest, requires adding external instrumentation.