---
{
  "title": "Agent Bridge Event Routing and Wire Type Tests",
  "summary": "This module verifies that `agent_bridge` routes all group broadcasts through `emit()` rather than directly calling `ws_manager.broadcast_to_group`, and that the four agent stream event types (`AgentStreamStart`, `AgentStreamChunk`, `AgentToolUse`, `AgentStreamEnd`) are correctly defined and importable from the bridge.",
  "concepts": [
    "agent_bridge",
    "emit",
    "ws_manager",
    "broadcast_to_group",
    "AgentStreamStart",
    "AgentStreamChunk",
    "AgentToolUse",
    "AgentStreamEnd",
    "event bus",
    "wire type",
    "static analysis",
    "regression guard"
  ],
  "categories": [
    "agent bridge",
    "realtime",
    "testing",
    "event routing",
    "test"
  ],
  "source_docs": [
    "1762883ca7a8cd2e"
  ],
  "backlinks": null,
  "word_count": 478,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/shared/test_agent_bridge_emits.py` module enforces the architectural constraint that the `agent_bridge` module must use the event bus (`emit()`) as its sole broadcast mechanism — not the WebSocket manager's `broadcast_to_group` directly.

## Why This Matters

The PocketPaw realtime architecture has two broadcast paths: the old `ws_manager.broadcast_to_group` (direct WebSocket fan-out) and the new event bus `emit()` (fan-out through the `AudienceResolver`). The bus path is preferred because it decouples message delivery from WebSocket session management, supports multiple transport backends, and enables proper audience scoping. If `agent_bridge` called `ws_manager` directly, scaling to multiple server instances or swapping transports would silently break — events would reach only the sockets on the same process.

## Test 1: emit() Routing Smoke Test

`test_agent_bridge_emits_stream_start_through_bus` patches `ee.cloud.shared.agent_bridge.emit` and then directly calls `agent_bridge.emit(AgentStreamStart(...))`. This confirms that `agent_bridge` imports the exact same `emit` symbol that is being patched — if the bridge imported a different `emit` from a different module path, the patch would not intercept it and the test would fail.

## Test 2: Static Regression Guard

`test_agent_bridge_does_not_import_ws_manager_broadcast_directly` reads the source file and asserts the string `ws_manager.broadcast_to_group` does not appear in it. This is a static analysis guard — it does not run the code, it inspects the text. This prevents a future developer from accidentally reverting to the old broadcast pattern. If the text appears, the test fails with an explicit message explaining that the call must be routed through `emit()` instead.

Note: the test uses a Windows-style path `D:/paw/backend/ee/cloud/shared/agent_bridge.py`, which may need to be updated for cross-platform compatibility.

## Test 3: Wire Type Verification

`test_agent_bridge_emit_calls_preserve_wire_types` iterates over the four expected agent event classes and asserts that each has the correct `EVENT_TYPE` class attribute:

```python
expected_classes = {
    "AgentStreamStart": "agent.stream_start",
    "AgentStreamChunk": "agent.stream_chunk",
    "AgentToolUse": "agent.tool_use",
    "AgentStreamEnd": "agent.stream_end",
}
```

This guards against renaming wire types without updating all consumers. Clients subscribe to specific event type strings; a rename without this test would silently break client-side event handling.

## Relationship to the Broader Architecture

These three tests together enforce a layered constraint on `agent_bridge`: it must import from `ee.cloud.realtime.events`, call `emit()`, and never bypass the bus by reaching into `ws_manager` directly. This separation of concerns is what allows the realtime backend to be swapped (e.g., from in-process to Redis) without touching the bridge code. If the bridge directly called WebSocket methods, every transport change would require auditing and updating the bridge — creating tight coupling between the agent execution layer and the delivery infrastructure.

## Known Gaps

The static path check (`D:/paw/backend/...`) is hardcoded to a Windows path, which will fail on Linux/macOS CI if the source root differs. This is a portability concern that should be replaced with a path relative to the project root or a `__file__`-based lookup. The tests do not verify that the full `_run_agent_response` flow actually reaches `emit()` for all four event types — only that the classes are importable and correctly typed.