---
{
  "title": "A2A JSON-RPC Dispatcher: Method Registration and Request Routing",
  "summary": "`A2ADispatcher` parses incoming JSON-RPC 2.0 requests, validates the envelope, and routes them to registered async handlers. It supports both blocking dispatch and SSE streaming dispatch, batch requests, and consistent error handling — with a deliberate separation between stream-only and non-stream methods.",
  "concepts": [
    "A2ADispatcher",
    "JSON-RPC 2.0",
    "method dispatch",
    "batch requests",
    "SSE streaming",
    "envelope validation",
    "stream methods",
    "error handling",
    "register",
    "register_stream",
    "notifications"
  ],
  "categories": [
    "A2A protocol",
    "JSON-RPC",
    "routing",
    "streaming"
  ],
  "source_docs": [
    "fb97f9d48d6ccd3d"
  ],
  "backlinks": null,
  "word_count": 435,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Architecture

`A2ADispatcher` is the central routing layer for all A2A protocol calls. It maintains two handler registries:

- `_methods`: async functions for blocking JSON-RPC methods
- `_stream_methods`: async generator functions for streaming (SSE) methods

Handlers are registered at startup via `register()` and `register_stream()`. The dispatcher is stateless between requests — it holds no per-request context.

## Envelope Validation

All incoming requests pass through `_validate_envelope` before dispatch:

```python
def _validate_envelope(self, obj: dict) -> tuple[str, dict, int | str | None]:
    if obj.get("jsonrpc") != "2.0": raise JSONRPCError(INVALID_REQUEST, ...)
    method = obj.get("method")
    if not method or not isinstance(method, str): raise JSONRPCError(...)
    params = obj.get("params", {})
    if not isinstance(params, dict): raise JSONRPCError(INVALID_PARAMS, ...)
    return method, params, obj.get("id")
```

This validates the three structural requirements of JSON-RPC 2.0: the version field, a string method name, and an object params. Extracting `request_id` here ensures it is available even if subsequent handler errors need to reference it.

## Batch Request Support

```python
if isinstance(parsed, list):
    results = []
    for item in parsed:
        result = await self._dispatch_single(item)
        if result is not None:  # notifications (no id) return None
            results.append(result)
```

Batch requests (arrays of JSON-RPC objects) are processed sequentially. The JSON-RPC spec allows notification requests (no `id`) — these are fire-and-forget and return `None`, which is filtered from the results list.

## Stream vs Non-Stream Separation

The dispatcher enforces a hard separation between stream and non-stream methods:

- Calling a stream method via `dispatch()` (blocking) returns an error: `"Method requires streaming. Use the SSE endpoint."`
- Calling a non-stream method via `dispatch_stream()` (SSE) executes it and wraps the result in a single event — graceful fallback.

This asymmetry is intentional: a streaming method producing real-time events cannot be safely buffered into a blocking response (it might never terminate). But a blocking method can always be served over SSE as a single event.

## Error Handling Hierarchy

Two error boundaries exist in `_dispatch_single`:

1. `except JSONRPCError` — expected protocol errors (wrong method, bad params). Serialized via `exc.to_response(request_id)`.
2. `except Exception` — unexpected internal errors. Logged with `logger.exception()` and returned as a generic `INTERNAL_ERROR` response.

This ensures the dispatcher always returns a valid JSON-RPC envelope, never leaking raw Python exceptions to the caller.

## Streaming Error Handling

`dispatch_stream` uses the same two-tier error handling but yields errors as events rather than returning them. This means even a mid-stream internal error produces a valid JSON-RPC error event that the SSE client can parse.

## Known Gaps

The dispatcher processes batch requests sequentially, not concurrently. For high-throughput batch calls, parallel dispatch with `asyncio.gather` would improve latency. The current implementation prioritizes simplicity and correct ordering over throughput.