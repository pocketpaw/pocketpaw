---
{
  "title": "emit() Facade Tests: Bus Delegation, Error Swallowing, and Initialization Guard",
  "summary": "These tests verify the `emit()` top-level facade that service code calls to publish real-time events. The suite ensures emit delegates unchanged event objects to the active bus, swallows bus errors to protect callers from infrastructure failures, and raises AssertionError when called before the bus singleton is initialized.",
  "concepts": [
    "emit facade",
    "bus delegation",
    "error swallowing",
    "initialization guard",
    "AssertionError",
    "set_bus",
    "InProcessBus",
    "real-time publishing",
    "best-effort delivery",
    "facade pattern"
  ],
  "categories": [
    "testing",
    "real-time",
    "event bus",
    "facade pattern",
    "test"
  ],
  "source_docs": [
    "bea598d5dff10c25"
  ],
  "backlinks": null,
  "word_count": 292,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`emit()` is the single import that all service-layer code uses to publish real-time events. It is a thin facade over the bus singleton: it gets the current bus and calls `publish()`. Keeping this as a separate function makes it easy to mock in tests and provides a centralized place for cross-cutting concerns like logging or metrics.

## Delegation to Active Bus

```python
async def test_emit_delegates_to_active_bus():
    stub_bus = AsyncMock()
    set_bus(stub_bus)
    ev = GroupCreated(data={"group_id": "g", "member_ids": ["u1"]})
    await emit(ev)
    stub_bus.publish.assert_awaited_once_with(ev)
```

`emit` passes the event object through unchanged — no transformation, no copying. The bus receives the exact same object reference.

## Error Swallowing

```python
async def test_emit_swallows_bus_errors():
    class BrokenBus:
        async def publish(self, _ev):
            raise RuntimeError("redis offline")
    set_bus(BrokenBus())
    # Must not raise
    await emit(GroupCreated(data={"group_id": "g", "member_ids": ["u1"]}))
```

Service code calls `emit()` as a side effect after completing a mutation. If emit propagated bus exceptions, a Redis outage would cause every chat message to fail even though the message was successfully saved to MongoDB. By swallowing bus errors, `emit` ensures real-time delivery is best-effort and never blocks the primary operation.

## Initialization Guard

```python
async def test_emit_raises_if_bus_not_initialized():
    bus_mod._bus = None
    with pytest.raises(AssertionError):
        await emit(GroupCreated(data={"group_id": "g", "member_ids": []}))
```

If `emit` is called before the application initializes the bus (via `set_bus()`), it raises `AssertionError`. This is a wiring bug, not a recoverable runtime condition — it surfaces immediately in development rather than silently doing nothing.

## Error-Handling Layering

The split between `emit` and `InProcessBus` follows a clear principle:
- `emit` swallows errors from the bus.
- `InProcessBus` swallows per-recipient delivery errors and audience resolution errors.
- Nothing propagates up to the service layer.

From the service layer's perspective, `await emit(ev)` is always safe to call after any mutation.

## Known Gaps

None identified.