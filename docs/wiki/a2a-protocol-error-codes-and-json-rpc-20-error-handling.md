---
{
  "title": "A2A Protocol Error Codes and JSON-RPC 2.0 Error Handling",
  "summary": "This module defines the full error code registry for the A2A protocol layer — combining standard JSON-RPC 2.0 error codes with A2A-specific extensions — and provides the `JSONRPCError` exception class and helper functions for building well-formed error and success response envelopes.",
  "concepts": [
    "JSON-RPC 2.0",
    "error codes",
    "JSONRPCError",
    "PARSE_ERROR",
    "METHOD_NOT_FOUND",
    "TASK_NOT_FOUND",
    "A2A protocol",
    "error envelope",
    "to_response",
    "json_rpc_error_response"
  ],
  "categories": [
    "A2A protocol",
    "error handling",
    "JSON-RPC"
  ],
  "source_docs": [
    "a9d76eb15e07527a"
  ],
  "backlinks": null,
  "word_count": 388,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Standard JSON-RPC 2.0 Error Codes

The JSON-RPC 2.0 specification reserves error codes in the `-32xxx` range:

| Code | Meaning |
|------|---------|
| `-32700` | Parse error — invalid JSON |
| `-32600` | Invalid request — not a valid JSON-RPC object |
| `-32601` | Method not found |
| `-32602` | Invalid params — params failed validation |
| `-32603` | Internal error — unexpected server error |

These constants are used by `A2ADispatcher` when validating the JSON-RPC envelope and dispatching to handlers.

## A2A-Specific Error Codes

The A2A protocol extends JSON-RPC with task-lifecycle error codes in the `-32001` to `-32005` range:

| Code | Meaning |
|------|---------|
| `-32001` | Task not found |
| `-32002` | Task not cancelable (already terminal) |
| `-32003` | Task not modifiable (in a terminal state) |
| `-32004` | Unsupported operation |
| `-32005` | Incompatible output modes |

These allow clients to distinguish between protocol errors (malformed request) and business-logic errors (trying to cancel a completed task).

## `JSONRPCError` Exception

`JSONRPCError` is a Python exception that carries the full JSON-RPC error payload:

```python
class JSONRPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        ...
    def to_response(self, request_id) -> dict:
        ...
```

The `to_response` method serializes the exception directly into a JSON-RPC 2.0 error envelope. This pattern allows `A2ADispatcher` to catch `JSONRPCError` at a single point and serialize it, rather than having each handler build its own error response shape.

## Helper Functions

Two stateless helpers build response envelopes for common cases:

```python
json_rpc_error_response(request_id, code, message, data=None) -> dict
json_rpc_success_response(request_id, result) -> dict
```

These are used when the error arises outside of a `JSONRPCError` exception (e.g., parse failures before `request_id` is known — in which case `request_id=None` is passed).

## Why `data` is Optional

The `data` field in JSON-RPC error objects is optional. Including it adds detail (e.g., which field failed validation) but also risks leaking internal implementation details to external callers. The default of `None` means `data` is omitted from the response unless explicitly provided.

## Known Gaps

No HTTP status code mapping is defined here — the A2A server layer is responsible for choosing an appropriate HTTP status (e.g., `400` for parse errors, `404` for task not found) based on the JSON-RPC code. That mapping is not centralized in this module.