---
{
  "title": "Cloud Error Hierarchy — Unified Exception Types for the Cloud Module",
  "summary": "Defines a structured exception hierarchy for the cloud module with machine-readable error codes and HTTP status codes. All cloud domains raise these instead of raw HTTPException, ensuring consistent error handling, logging, and API response formatting.",
  "concepts": [
    "error hierarchy",
    "CloudError",
    "NotFound",
    "Forbidden",
    "ConflictError",
    "ValidationError",
    "SeatLimitError",
    "machine-readable error codes",
    "HTTP status codes"
  ],
  "categories": [
    "cloud",
    "shared",
    "error handling",
    "API design"
  ],
  "source_docs": [
    "71268e8625ce7bff"
  ],
  "backlinks": null,
  "word_count": 287,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Cloud Error Hierarchy — Unified Exception Types for the Cloud Module

## Purpose

Rather than scattering `HTTPException(status_code=404, detail="...")` calls throughout the codebase, all cloud domain code raises typed exceptions from this hierarchy. This provides:

1. **Consistent API responses** — every error has a machine-readable `code` and human-readable `message`
2. **Centralized handling** — a single exception handler can catch `CloudError` and format responses uniformly
3. **Domain-specific semantics** — `NotFound("session", "abc123")` is more expressive than `HTTPException(404)`

## Exception Classes

### CloudError (Base)

All cloud exceptions inherit from this. Properties:
- `status_code` (int) — HTTP status code
- `code` (str) — machine-readable identifier like `"session.not_found"` or `"billing.seat_limit"`
- `message` (str) — human-readable description
- `to_dict()` — returns `{"error": {"code": ..., "message": ...}}` for JSON responses

### NotFound (404)

Auto-generates the code as `"{resource}.not_found"` and the message as `"{resource} '{id}' not found"`. Usage: `raise NotFound("session", session_id)`.

### Forbidden (403)

For access control violations. Takes a custom code and message. Usage: `raise Forbidden("session.not_owner", "Not the session owner")`.

### ConflictError (409)

For duplicate resources or state conflicts. Usage: `raise ConflictError("workspace.name_taken", "Workspace name already exists")`.

### ValidationError (422)

For business rule validation failures beyond what Pydantic catches. Not to be confused with `pydantic.ValidationError` — this is for domain-level validation.

### SeatLimitError (402)

Billing-specific: raised when a workspace tries to add members beyond its seat limit. Uses HTTP 402 (Payment Required) with the code `"billing.seat_limit"`.

## Design Notes

The `code` field follows a `domain.error_type` convention (e.g., `session.not_found`, `workspace.not_member`, `billing.seat_limit`). This allows frontends to match on error codes for localization or custom handling without parsing message strings.

The `to_dict()` method wraps the error in an `{"error": {...}}` envelope, matching a common REST API convention that distinguishes error responses from success responses at the top level.
