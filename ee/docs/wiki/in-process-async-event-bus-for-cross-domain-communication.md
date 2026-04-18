---
{
  "title": "In-Process Async Event Bus for Cross-Domain Communication",
  "summary": "Provides a simple pub/sub event bus that allows cloud domains to react to events from other domains without importing each other directly. Handlers are awaited sequentially with per-handler error isolation to prevent one failing handler from breaking others.",
  "concepts": [
    "event bus",
    "pub/sub",
    "async event handling",
    "error isolation",
    "loose coupling",
    "domain events",
    "singleton pattern"
  ],
  "categories": [
    "cloud",
    "shared",
    "architecture",
    "event system",
    "design patterns"
  ],
  "source_docs": [
    "f8d182f8a25beedd"
  ],
  "backlinks": null,
  "word_count": 386,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# In-Process Async Event Bus for Cross-Domain Communication

## Purpose

The event bus enables loose coupling between cloud domains. Instead of the sessions service importing the notifications module to create a notification on session creation, it emits a `"session.created"` event. Any module can subscribe to that event without the emitter knowing about it.

This is the architectural backbone of PocketPaw's cloud side-effect system. Events like `invite.accepted`, `message.sent`, `pocket.shared`, and `member.removed` all flow through this bus.

## Implementation

### EventBus Class

A straightforward pub/sub with three operations:

- **`subscribe(event, handler)`** — registers an async handler for a named event
- **`unsubscribe(event, handler)`** — removes a handler (no-op if not found, preventing errors during teardown)
- **`emit(event, data)`** — awaits every registered handler for the event in subscription order

### Error Isolation

The critical design choice: each handler is called in its own try/except block within `emit()`. If one handler raises, the exception is logged and remaining handlers still execute. This prevents a bug in one subscriber from breaking all other side effects.

For example, if the notification handler crashes when processing `message.sent`, the group stats handler still runs.

### Handler Type

Handlers must match the `Handler` type alias: `Callable[[dict[str, Any]], Coroutine[Any, Any, None]]` — an async function that takes a dict and returns nothing.

### Module-Level Singleton

`event_bus = EventBus()` at module scope creates a single shared instance. All cloud modules import and use this same instance:

```python
from ee.cloud.shared.events import event_bus
```

## Design Notes

This is intentionally simple — no message queues, no persistence, no replay. It's an in-process pub/sub designed for side effects that can tolerate occasional failures (notifications, stats updates). Critical operations should not rely solely on the event bus.

Handlers run sequentially (awaited in order), not concurrently. This simplifies reasoning about handler interactions but means a slow handler blocks subsequent ones for the same event.

## Known Gaps

- No concurrent handler execution — a slow handler delays all subsequent handlers for the same event
- No event persistence or replay — events are fire-and-forget; if the process crashes mid-emit, remaining handlers don't run
- No wildcard subscriptions (e.g., `"session.*"`) — each event must be subscribed individually
- No mechanism to inspect registered handlers for debugging
- `unsubscribe` uses `list.remove()` which is O(n) — fine for the small handler counts in practice
