---
{
  "title": "Realtime WebSocket Event Package Entry Point",
  "summary": "This is the package initialiser for the `ee.cloud.realtime` module, which houses the WebSocket-based realtime event system for the EE cloud tier. The file itself carries only a module docstring, serving as the namespace anchor that makes `ee.cloud.realtime` importable as a package while the substantive components live in sub-modules.",
  "concepts": [
    "realtime package",
    "WebSocket",
    "event system",
    "package initialiser",
    "InProcessBus",
    "RedisBus",
    "EventBus protocol",
    "cross-cutting concerns",
    "sub-module organisation"
  ],
  "categories": [
    "realtime",
    "WebSocket",
    "EE cloud",
    "package structure"
  ],
  "source_docs": [
    "5a18f9fafb129648"
  ],
  "backlinks": null,
  "word_count": 325,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/realtime/__init__.py` is the entry point for the `ee.cloud.realtime` package. Its sole content is a module docstring: `"Realtime WebSocket event package for EE cloud."` There is no re-export surface here — callers import directly from the sub-modules (`bus`, `emit`, `events`, `audience`).

## Why a Separate Package?

The decision to organise realtime concerns as a dedicated package (rather than placing them in, say, `ee/cloud/chat/`) reflects the cross-cutting nature of the realtime system. Events originate from multiple unrelated domains — sessions, pockets, messages, workspace membership, agent pipelines — and the audience resolver needs to fan out to all connected users regardless of which domain triggered the change. Keeping the package separate prevents circular imports that would arise if, for example, the message service imported from `ee.cloud.chat` and `ee.cloud.chat` imported the event bus back.

## Package Structure

The sub-modules are:

- **`events.py`** — Dataclass-based event type catalogue. Every emittable event has a typed class with a `ClassVar` `EVENT_TYPE` string.
- **`bus.py`** — `EventBus` protocol plus `InProcessBus` implementation and module-level singleton management.
- **`emit.py`** — Thin public facade. Services call `emit(event)` here rather than touching the bus directly.
- **`audience.py`** — `AudienceResolver` that maps an event to the list of user IDs that should receive it.

## Design Intent

The package is designed to be swap-out friendly at the bus level. The `InProcessBus` works for single-process deployments (development, tests, small-scale). A planned `RedisBus` (noted in the bus module as Task 33) will implement the same `EventBus` protocol, allowing all call sites that use `emit()` to remain unchanged when the backend is switched.

The empty `__init__.py` pattern (no re-exports) is intentional — it avoids import order issues during pytest collection, which was noted as a real problem with the `WsOutbound` import in `bus.py`.

## Known Gaps

- **No `__all__`**: The package exposes no explicit public surface via `__all__`. Consumers must know which sub-module to import from.
- **RedisBus not yet implemented**: The planned multi-process bus (Task 33) remains outstanding.