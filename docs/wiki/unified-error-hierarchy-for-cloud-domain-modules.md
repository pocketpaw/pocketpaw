---
{
  "title": "Unified Error Hierarchy for Cloud Domain Modules",
  "summary": "This module defines a structured exception hierarchy used across all cloud EE domain packages instead of raw `HTTPException` raises. It standardizes error shape, HTTP status codes, and machine-readable error codes in one place, ensuring consistent API responses and centralized error handling.",
  "concepts": [
    "CloudError",
    "NotFound",
    "Forbidden",
    "ConflictError",
    "ValidationError",
    "SeatLimitError",
    "HTTPException",
    "error hierarchy",
    "machine-readable error code",
    "error envelope"
  ],
  "categories": [
    "error handling",
    "API design",
    "cloud EE"
  ],
  "source_docs": [
    "71268e8625ce7bff"
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

`ee/cloud/shared/errors.py` provides the base `CloudError` class and a set of domain-specific subclasses that every cloud package raises when something goes wrong. The goal is to avoid each domain independently crafting `HTTPException` calls with inconsistent status codes and unstructured messages.

## Why a Custom Hierarchy Instead of HTTPException

FastAPI's `HTTPException` carries a `detail` string and a status code, but it has no machine-readable error code. A client that receives a 404 from a generic `HTTPException` cannot distinguish between "agent not found" and "workspace not found" without parsing the detail string — which is fragile. `CloudError` adds a `code` field (e.g., `"agent.not_found"`) that clients can match programmatically.

Additionally, raising `HTTPException` directly from domain services couples those services to the HTTP transport layer. A service that raises `NotFound` can be tested without a running FastAPI app, and a future gRPC or CLI adapter can catch the same exception and map it to the appropriate transport error.

## Error Envelope Shape

Every `CloudError` serializes to a consistent JSON envelope via `to_dict()`:

```python
{"error": {"code": "agent.not_found", "message": "agent not found"}}
```

This shape is consumed by the global exception handler in the FastAPI app, which calls `to_dict()` and sets the response status from `error.status_code`. No router needs to format its own error response.

## Subclass Inventory

- **`NotFound` (404)** — generated code is `"{resource}.not_found"`, message includes the resource ID when available. Prevents callers from constructing inconsistent 404 messages.
- **`Forbidden` (403)** — access denied with caller-supplied code and message, used by service-layer authorization checks.
- **`ConflictError` (409)** — resource already exists or state conflict, e.g., duplicate workspace slug.
- **`ValidationError` (422)** — input failed business-rule validation beyond what Pydantic catches at the schema level.
- **`SeatLimitError` (402)** — billing enforcement: the workspace has reached its licensed seat count. Using HTTP 402 signals to the client that this is a billing gate rather than a permissions error.

## Defensive Design

The base `__init__` calls `super().__init__(f"{code}: {message}")`, which sets the standard `Exception` message. This means `str(err)` and `repr(err)` are informative in logs without any additional formatting, and the exception can be caught and re-raised through layers that only care about the base `Exception` type without losing context.

## Known Gaps

- There is no `UnauthorizedError` (401) subclass. Unauthenticated requests are handled by the FastAPI authentication layer directly, which raises a plain `HTTPException(401)`. If authentication errors ever need machine-readable codes, a new subclass would be needed.
- `to_dict()` does not include `status_code` in the response body. Clients relying solely on the JSON body cannot determine the error category without mapping code prefixes manually.