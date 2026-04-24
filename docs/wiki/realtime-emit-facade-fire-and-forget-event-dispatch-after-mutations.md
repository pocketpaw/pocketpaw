---
{
  "title": "Realtime emit() Facade: Fire-and-Forget Event Dispatch After Mutations",
  "summary": "The `emit` function is a thin, fire-and-forget facade that service methods call after successful database writes to push realtime events to connected clients. It never raises exceptions back to the caller — a delivery failure must not roll back or abort the preceding database mutation — while a missing bus initialisation surfaces immediately as an AssertionError.",
  "concepts": [
    "emit",
    "fire-and-forget",
    "realtime facade",
    "EventBus",
    "AssertionError guard",
    "best-effort delivery",
    "service pattern",
    "mutation-then-emit",
    "error swallowing",
    "get_bus"
  ],
  "categories": [
    "realtime",
    "event dispatch",
    "EE cloud",
    "service pattern"
  ],
  "source_docs": [
    "249b73b17a2e90ff"
  ],
  "backlinks": null,
  "word_count": 384,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/realtime/emit.py` contains a single exported async function: `emit(event: Event) -> None`. Its entire purpose is to provide a clean, safe call site for services that want to push a realtime event after completing a database write.

## Why a Separate Facade?

Services could call `get_bus().publish(event)` directly, but the `emit` facade provides two guarantees that make call sites simpler and safer:

1. **Never raises**: Any exception from `bus.publish` is caught and logged at `exception` level. This protects the calling service from having a WebSocket delivery failure corrupt a successful database write. The pattern "write to DB, then emit event" is common throughout the codebase, and without this guarantee every call site would need its own try/except.

2. **Fails loudly on uninitialised bus**: `get_bus()` raises `AssertionError` if the bus has not been initialised. This is deliberately not caught by `emit`, so forgetting to call `init_realtime()` at startup surfaces immediately in tests rather than being silently swallowed.

```python
async def emit(event: Event) -> None:
    bus = get_bus()  # raises AssertionError if not initialized
    try:
        await bus.publish(event)
    except Exception:
        logger.exception("emit failed for event %s", event.type)
```

## Caller Pattern

A typical call site looks like:

```python
await session.save()
await emit(SessionCreated(data={"session_id": str(session.id), "user_id": user_id}))
```

The ordering is important: `emit` is always called _after_ the database operation succeeds. This means a realtime event is never sent for a mutation that was rolled back, but it also means clients may momentarily see stale state if the emit fails — the source of truth is always the database.

## Error Logging

Failed emissions are logged at `exception` level (with full traceback). This is intentionally louder than the per-user `warning` level used inside `InProcessBus.publish` — an emission failure means the entire fan-out was skipped, whereas a per-user send failure is a partial delivery issue.

## Known Gaps

- **No retry**: Failed events are dropped. High-importance events (e.g., `session.deleted`) will not be retried if the bus fails.
- **No event queue**: There is no buffering. If the bus is temporarily overloaded or the connection manager is mid-restart, events are lost.
- **Best-effort semantics are not documented at call sites**: Callers are expected to know that emit is best-effort. A developer who is not aware of this contract might incorrectly assume that a successful `await emit(...)` means all clients received the event.