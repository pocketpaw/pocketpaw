---
{
  "title": "Agent Loop Tests: Message Processing, AgentEvent Architecture, Error Handling, and GC Lock Management",
  "summary": "Tests for the unified `AgentLoop` that orchestrates message consumption, agent router invocation, memory management, and outbound publishing. Updated for the `AgentEvent`-based streaming architecture (replacing raw dict chunks), these tests verify end-to-end message processing, graceful error handling, stale-lock GC, and loop teardown.",
  "concepts": [
    "AgentLoop",
    "AgentEvent",
    "message processing",
    "GC lock reaping",
    "mock bus",
    "mock memory",
    "agent router",
    "outbound publishing",
    "async streaming",
    "concurrent locks"
  ],
  "categories": [
    "testing",
    "agent loop",
    "core runtime",
    "concurrency",
    "test"
  ],
  "source_docs": [
    "5be0d14b80c96a54"
  ],
  "backlinks": null,
  "word_count": 475,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_agent_loop.py` covers `pocketpaw.agents.loop.AgentLoop`, the central coordinator that bridges the message bus (inbound user messages) with the agent router (Claude SDK or other backend), memory manager, and outbound bus publishing. The file was updated from the earlier dict-chunk streaming model to the typed `AgentEvent` protocol.

## Why AgentEvent Replaces Dict Chunks

The previous architecture streamed raw dicts (`{"type": "message", "content": "..."}`) from the router to the loop. This was fragile — callers had to check `chunk.get("type")` defensively, and missing keys caused silent drops rather than type errors. `AgentEvent` is a Pydantic-lite dataclass:

```python
AgentEvent(type="message", content="Hello ")
AgentEvent(type="tool_use", content="Using test_tool...", metadata={"name": "test_tool", "input": {}})
AgentEvent(type="done", content="")
```

The typed contract means any backend that does not emit a `done` event will leave the loop spinning — a detectable error rather than a silent hang.

## Test Breakdown

### test_agent_loop_process_message
The primary integration test. Mocks all four external dependencies (bus, memory, context builder, router) and calls `loop._process_message(msg)`. Asserts:
- `memory.add_to_session` is called (history is persisted).
- `bus.publish_outbound` is called at least twice (streamed message chunks reach the channel).
- `bus.publish_system` is called at least once (tool use / system events are broadcast).

The mock router yields a realistic sequence: two message chunks, one tool_use event, one tool_result event, and a done sentinel.

### test_agent_loop_reset_router
Verifies that `reset_router()` clears the cached router instance. This is used when the agent backend changes at runtime (e.g., user switches from Claude SDK to a different backend in settings). The test confirms `_router` is `None` both before initialisation and after reset — no stale instance leaks.

### test_agent_loop_handles_error
The error router yields an `AgentEvent(type="error")` followed by `done`. The loop must not raise; it must publish the error to the outbound bus so the frontend can display it, then complete normally.

### GC Lock Tests
`AgentLoop` uses per-conversation locks to prevent concurrent processing of messages for the same chat. The GC (garbage collection) task periodically reaps locks for conversations that have been idle too long:

```python
async def test_gc_removes_stale_locks():
    # Lock created > GC threshold ago should be reaped.
    ...
async def test_gc_skips_acquired_locks():
    # An actively held lock must not be reaped mid-conversation.
    ...
async def test_stop_cancels_gc_task():
    # loop.stop() must cancel the background GC coroutine.
    ...
```

Without stale-lock reaping, long-running deployments would accumulate one lock per historical conversation, slowly exhausting memory. The `test_gc_skips_acquired_locks` test prevents the GC from introducing a race condition where an in-progress conversation loses its lock.

## Fixture Anatomy

Three fixtures (`mock_bus`, `mock_memory`, `mock_router`) provide `AsyncMock`-based doubles. The `_make_loop_with_settings` helper patches the four module-level singletons (`get_message_bus`, `get_memory_manager`, `AgentContextBuilder`, `AgentRouter`) simultaneously to avoid brittle import-patch ordering.

## Known Gaps

No test covers concurrent `_process_message` calls for the same `chat_id` — the lock contention path that the GC tests indirectly protect is not exercised directly. The `history compaction` path (`get_compacted_history`) is mocked to return an empty list in all tests.