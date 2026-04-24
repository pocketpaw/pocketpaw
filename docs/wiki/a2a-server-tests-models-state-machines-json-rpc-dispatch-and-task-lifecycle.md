---
{
  "title": "A2A Server Tests: Models, State Machines, JSON-RPC Dispatch, and Task Lifecycle",
  "summary": "Extensive tests for the PocketPaw A2A server implementation covering Pydantic model serialisation with type discriminators, TaskState transition validation, JSON-RPC 2.0 dispatcher behaviour (parse errors, batch requests, method routing), and the full task lifecycle (submit, poll, stream, cancel) against a minimal in-memory FastAPI app.",
  "concepts": [
    "A2A server",
    "JSON-RPC 2.0",
    "task state machine",
    "TaskState",
    "discriminator",
    "streaming SSE",
    "A2ADispatcher",
    "TaskSendParams",
    "AgentCard",
    "FastAPI middleware"
  ],
  "categories": [
    "testing",
    "A2A protocol",
    "agent communication",
    "API server",
    "test"
  ],
  "source_docs": [
    "8e56c94a37709e80"
  ],
  "backlinks": null,
  "word_count": 502,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_a2a_server.py` covers Phase 1 of the A2A protocol server implementation. The A2A protocol exposes PocketPaw as an agent that remote callers can invoke over HTTP using either a REST task API or a JSON-RPC 2.0 endpoint. The test file was created on 2026-03-07 and covers every layer from model validation through to end-to-end task streaming.

## Test Structure

### TestModels — Pydantic Validation and Discriminators

The A2A protocol defines a polymorphic `Part` type with three variants: `TextPart`, `FilePart`, and `DataPart`. Each carries a `type` field used as a Pydantic discriminator:

```python
def test_part_discriminator(self):
    msg = A2AMessage(role="agent", parts=[
        TextPart(text="hello"),
        DataPart(data={"x": 1}),
    ])
    data = msg.model_dump()
    assert data["parts"][0]["type"] == "text"
    assert data["parts"][1]["type"] == "data"
```

Without discriminator-aware serialisation, a remote caller deserialising the response would not know which `Part` subclass to instantiate. Tests also verify `FilePart` byte encoding, `AgentCard` default fields, and the auto-generated `id` on `TaskSendParams`.

### TestStateTransitions — Task State Machine

A2A tasks follow a strict state machine: `submitted → working → {completed, failed, canceled, input-required}`. The `validate_transition` function enforces valid transitions and raises on invalid ones. Tests cover every legal edge and several illegal ones:
- Terminal states (`completed`, `failed`, `canceled`, `rejected`) cannot transition further.
- `input-required → working` is the only allowed transition from `input-required` (re-activation after user input).
- `submitted → rejected` is valid (pre-flight rejection by the server).

Without this state machine, a remote caller could observe inconsistent task states and retry a task that had already completed.

### TestJSONRPCModels
JSON-RPC 2.0 request/response structures must serialise correctly, including error objects with optional `data` payloads. These are the wire format contracts with every external A2A caller.

### TestStreamingEvents
`TaskStatusUpdateEvent` and `TaskArtifactUpdateEvent` are the SSE frame types emitted during task streaming. Tests verify the required fields (`id`, `status`, `final`) and that partial artifact chunks are distinguishable from the final chunk.

### TestErrors
Error codes follow JSON-RPC 2.0 conventions plus A2A-specific codes (`TASK_NOT_FOUND`, `TASK_NOT_CANCELABLE`, `UNSUPPORTED_OPERATION`). The `JSONRPCError` exception class must convert cleanly to an error response object.

### TestA2ADispatcher — JSON-RPC Routing

The dispatcher maps JSON-RPC method names to registered handlers:
- Parse errors (invalid JSON) → error code `-32700`.
- Invalid requests (missing `jsonrpc` or `method` fields) → error code `-32600`.
- Unknown methods → error code `-32601`.
- Valid calls → handler invoked and result returned.
- Batch requests → each item dispatched independently.
- Empty batch → error (JSON-RPC 2.0 forbids empty batch arrays).

### TestA2AServer — Full Task Lifecycle

The `test_app` fixture mounts all three routers (`well_known_router`, `tasks_router`, `jsonrpc_router`) on a minimal FastAPI app with auth middleware mocked:

```python
@app.middleware("http")
async def mock_auth_middleware(request, call_next):
    class MockAPIKey:
        scopes = ["chat", "admin"]
    request.state.api_key = MockAPIKey()
    return await call_next(request)
```

The `clear_task_store` autouse fixture resets the in-memory `_tasks` dict and `_cancel_events` between tests to prevent state leakage.

## Known Gaps

The `_A2ASessionBridge` is imported but not extensively tested — its behaviour under concurrent requests is not covered. No test verifies that the `A2ADispatcher` correctly handles a JSON-RPC request with `id: null` (notification — should not return a response).