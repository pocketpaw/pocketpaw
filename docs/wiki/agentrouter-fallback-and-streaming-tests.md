---
{
  "title": "AgentRouter Fallback and Streaming Tests",
  "summary": "This test file validates `AgentRouter`'s fault-tolerant backend selection logic, covering hard exception failures, error-event failures, full backend chain exhaustion, streaming event pass-through, and no-fallback configurations. The tests use in-process stub backends injected via `monkeypatch` to exercise the router without real LLM backends.",
  "concepts": [
    "AgentRouter",
    "backend fallback",
    "error event",
    "streaming events",
    "AgentEvent",
    "backend registry",
    "fault tolerance",
    "LLM backend",
    "monkeypatch",
    "Settings",
    "async generator"
  ],
  "categories": [
    "testing",
    "agent routing",
    "fault tolerance",
    "streaming",
    "test"
  ],
  "source_docs": [
    "68ae0ca1c411c8ce"
  ],
  "backlinks": null,
  "word_count": 474,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_router_fallback.py` tests the `AgentRouter` from `pocketpaw.agents.router`. The router is responsible for selecting an agent backend (e.g., Claude, Ollama, a custom backend) and transparently falling back to alternatives when the primary fails. This resilience layer matters because LLM API outages, quota exhaustion, and network failures are routine in production.

## Stub Backend Classes

Four stub backends are defined at module scope:

- **`FailingBackend`** — raises `RuntimeError` immediately inside `run()`. Simulates a hard crash (no network, import error, crash).
- **`ErrorEventBackend`** — yields a single `AgentEvent(type="error", ...)` instead of raising. Some backends signal failure via events rather than exceptions; the router must detect both forms.
- **`StreamingBackend`** — yields three `message` events then a `done` event. Verifies that the router does not misinterpret intermediate events as failures and trigger a spurious fallback.
- **`SuccessBackend`** — always succeeds with one `message` and one `done` event. Used as the fallback target to confirm the router recovered.

Each stub implements the `AgentBackend` protocol: `info()`, `__init__(settings)`, async generator `run()`, and `stop()`.

## Test Scenarios

### Hard Exception Fallback

`test_router_fallback_success` registers `FailingBackend` as primary and `SuccessBackend` as fallback, then asserts that `"fallback worked"` appears in the collected events. The critical insight: if the router propagates the exception instead of catching it and trying the next backend, this test fails.

### Error Event Fallback

`test_router_error_event_fallback` registers `ErrorEventBackend` as primary. Because error events look like normal events to a naive consumer, the router must actively inspect event types and trigger fallback logic on `type="error"`. Without this, users would receive an error message from the primary and the fallback would never run.

### Full Chain Exhaustion

`test_router_all_backends_fail` registers `FailingBackend` as both primary and fallback. After all backends are exhausted, the router must emit an `error`-type event rather than raising or returning empty output. This gives the caller a structured error to display rather than a silent empty response.

### Streaming Without Spurious Fallback

`test_router_streaming_happy_path` is a regression guard: a backend emitting multiple `message` events before `done` should not trigger fallback. The test asserts that all three chunks arrive in order.

```python
contents = [e.content for e in events if e.type == "message"]
assert contents == ["chunk1", "chunk2", "chunk3"]
```

### No Fallback Configured

`test_router_no_fallback` verifies that with no `fallback_backends` set, a primary failure still results in a clean `error` event rather than an unhandled exception propagating to the caller.

## Registry Injection Pattern

All tests use `monkeypatch.setitem(registry._BACKEND_REGISTRY, ...)` to register stub backends by name. This avoids modifying real config files or environment variables and ensures the tests are fully isolated. `Settings` is constructed with explicit `agent_backend` and `fallback_backends` values, so no file I/O occurs.

## Known Gaps

No `TODO` or `FIXME` markers are present. The file does not test timeout-based fallback (where a backend hangs indefinitely rather than failing quickly), nor partial-response scenarios where some content was already streamed before the error.
