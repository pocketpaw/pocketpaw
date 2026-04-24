---
{
  "title": "Cloud Error Hierarchy: Structured HTTP Error Contract Tests",
  "summary": "This test file validates the `ee.cloud.shared.errors` module, which provides a typed exception hierarchy that maps Python exceptions to HTTP status codes and machine-readable error codes. It ensures that every error class produces the correct status, code, message, and serialization shape expected by API consumers.",
  "concepts": [
    "CloudError",
    "error hierarchy",
    "HTTP status codes",
    "machine-readable errors",
    "NotFound",
    "Forbidden",
    "ConflictError",
    "ValidationError",
    "SeatLimitError",
    "error serialization",
    "to_dict"
  ],
  "categories": [
    "testing",
    "error handling",
    "API design",
    "test"
  ],
  "source_docs": [
    "bcc6471126f1cad2"
  ],
  "backlinks": null,
  "word_count": 549,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's cloud API needs consistent, machine-readable errors that frontend clients and integrations can handle deterministically. The `ee.cloud.shared.errors` module defines a hierarchy of typed exceptions — each carrying an HTTP status code, a dot-separated error code, and a human-readable message. This test file locks in the contract for every error class in that hierarchy.

## Why This Exists

Without a typed error hierarchy, different routes would produce different error shapes — some returning `{"detail": "..."}` (FastAPI default), others returning raw strings, others raising bare Python exceptions. Clients would have to handle each inconsistency separately. By centralizing error types and testing them rigorously, the codebase guarantees that any caller catching `CloudError` or its subclasses gets a predictable shape.

## Error Classes Covered

**`CloudError`** — The base class. Carries `status_code`, `code`, and `message`. Inherits from `Exception` so it can be raised and caught anywhere. `test_cloud_error_is_exception` verifies this inheritance, which matters because FastAPI exception handlers catch `Exception` subclasses by type.

**`NotFound`** — HTTP 404. Generates a `<resource>.not_found` code automatically from the resource name. `test_not_found` verifies the code includes the resource name and the message includes the ID. `test_not_found_without_id` confirms it degrades gracefully when no ID is provided — the resource name still appears in the message.

**`Forbidden`** — HTTP 403. Defaults to a generic "Access denied" message to avoid information disclosure. `test_forbidden_custom_message` confirms a caller can override the message when more context is safe to share (e.g., internal tooling).

**`ConflictError`** — HTTP 409. Used when a unique constraint is violated (e.g., a workspace slug already taken). Carries a custom message describing which constraint failed.

**`ValidationError`** — HTTP 422. Used for semantic validation failures that pass Pydantic's structural validation but fail business logic (e.g., message too long, invalid combination of fields).

**`SeatLimitError`** — HTTP 402 (Payment Required). Indicates the tenant has hit its licensed seat cap. `test_seat_limit` verifies the seat count appears in the message, which is surfaced to the user as actionable information.

## Serialization Contract

`test_cloud_error_to_dict` pins the `to_dict()` shape:

```python
{"error": {"code": "group.not_found", "message": "..."}}
```

This envelope format is what the FastAPI exception handler serializes to JSON when it catches a `CloudError`. Clients key on `error.code` to dispatch to the right handler — not on the HTTP status alone, because multiple logical errors can share the same HTTP status.

`test_cloud_error_str` confirms that `str(err)` includes both the code and message. This matters for logging: when a `CloudError` bubbles up to a generic exception handler or gets logged, the string representation contains enough context for debugging without decrypting an object.

## Defensive Patterns

- **Generic Forbidden message** — The default "Access denied" message prevents privilege enumeration. A 403 from a route that checks workspace membership could leak whether a workspace exists if the message was "Workspace not found" vs "Not a member".
- **Typed error hierarchy** — Using a class hierarchy instead of ad-hoc dicts means MyPy and Pyright can catch callers that forget to handle specific error types.
- **`to_dict()` envelope** — The `{"error": {...}}` wrapper distinguishes error responses from success responses that happen to have similar fields.

## Known Gaps

No TODOs or FIXMEs observed. The hierarchy covers all HTTP error categories used in the cloud API (404, 403, 409, 422, 402). HTTP 500 is intentionally absent — internal errors should not be typed and surfaced to clients.
