---
{
  "title": "PocketPaw Cognitive Engine: Wiring Tests for Soul-Aware Thinking",
  "summary": "Tests for the `PocketPawCognitiveEngine`, which bridges the soul-protocol's identity layer with PocketPaw's LLM backend to produce soul-contextualised responses. Covers correct parameter forwarding, graceful error handling, stream termination logic, and end-to-end wiring through `SoulManager` and `AgentLoop`.",
  "concepts": [
    "PocketPawCognitiveEngine",
    "SoulManager",
    "AgentLoop",
    "backend.run",
    "stream accumulation",
    "soul-protocol",
    "think()",
    "cognitive engine",
    "graceful degradation"
  ],
  "categories": [
    "testing",
    "soul integration",
    "cognitive engine",
    "test"
  ],
  "source_docs": [
    "3d509531a291d1a3"
  ],
  "backlinks": null,
  "word_count": 427,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw separates *thinking* (calling the LLM) from *soul management* (maintaining identity, memory, and personality). The `PocketPawCognitiveEngine` is the bridge: it takes a soul's context and routes a query through the configured backend. This test file pins the contract of that bridge at multiple levels of the stack.

## Backend Invocation

`test_pocketpaw_engine_think` is the core test. It confirms that `think()` calls `backend.run()` exactly once and concatenates the text content from message-type events. This establishes the fundamental contract: the engine is a thin orchestration layer, not a transformer. The `_make_backend(*events)` helper creates a mock backend yielding a controlled sequence of events, keeping test setup readable.

`test_pocketpaw_engine_passes_correct_params` verifies that the `system_prompt` and `session_key` are forwarded to `backend.run()` unchanged. If either were dropped or transformed, the LLM would receive the wrong system context or the soul's session tracking would break.

## Graceful Degradation

Two tests address the "what if the backend isn't available" scenarios:

- `test_pocketpaw_engine_fallback_on_error`: When `backend.run()` raises an exception, `think()` must return an empty string instead of propagating. This prevents a backend outage from crashing the agent loop — the soul simply produces no output for that turn, which is recoverable.
- `test_pocketpaw_engine_no_backend`: When the backend provider returns `None` (e.g., not configured), `think()` returns `''` immediately without attempting a call. This guards against `NoneType` attribute errors that would be opaque to end users.

## Stream Termination

`test_pocketpaw_engine_done_event_stops_stream` and `test_pocketpaw_engine_stream_end_event` verify two alternative "stop accumulating" signals. The engine must stop collecting text when it sees either a `done`-type event or a `stream_end` event, whichever comes first. Without this, the engine would hang waiting for the async iterator to exhaust itself, blocking the agent loop.

`test_pocketpaw_engine_ignores_non_text_events` confirms that `tool_use`, `tool_result`, and similar non-text events are not concatenated into the response string. Mixing tool invocation data into the spoken response would produce garbled output.

## SoulManager Wiring

`test_soul_manager_initialize_passes_engine` verifies that `SoulManager.initialize()` forwards the engine to both `Soul.awaken()` (for existing souls) and `Soul.birth()` (for new souls). If the engine were not forwarded, new souls would have no thinking capability from birth.

## AgentLoop Integration

`test_agent_loop_builds_and_wires_engine` is the highest-level test: it confirms that `AgentLoop.start()` constructs a `PocketPawCognitiveEngine` and passes it to `SoulManager.initialize()`. This is the integration seam between the agent runtime and the soul layer.

## Known Gaps

None identified. The test coverage is methodical: happy path, two error paths, two stream-stop signals, non-text event filtering, and two levels of wiring (manager and loop).

```python
# Backend helper used across engine tests
def _make_backend(*events):
    backend = MagicMock()
    async def _run(**kwargs):
        for e in events:
            yield e
    backend.run = _run
    return backend
```
