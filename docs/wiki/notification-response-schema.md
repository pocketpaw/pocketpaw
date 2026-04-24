---
{
  "title": "Notification Response Schema",
  "summary": "`NotificationResponse` is the Pydantic response model for the notifications API, defining the wire format returned to frontend clients. It normalizes MongoDB document fields into a consistent camelCase-compatible shape with string-typed timestamps for JSON serialization safety.",
  "concepts": [
    "Pydantic response schema",
    "NotificationResponse",
    "wire format",
    "field normalization",
    "response model",
    "OpenAPI schema",
    "source_id",
    "type aliasing"
  ],
  "categories": [
    "notifications",
    "api-schemas",
    "enterprise-cloud"
  ],
  "source_docs": [
    "547669102754b1d6"
  ],
  "backlinks": null,
  "word_count": 384,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`NotificationResponse` is the API contract for notification payloads. It defines exactly what fields clients should expect when they call `GET /notifications`, making the response shape explicit and documentable independently of the internal `Notification` Beanie document.

## Why a Separate Response Schema?

The internal `Notification` document uses Beanie-specific types (`PydanticObjectId` for `_id`, `datetime` for timestamps, embedded `NotificationSource` objects) that do not serialize cleanly to JSON without transformation. The response schema:

1. **Normalizes `id`** — returns a `str` rather than a `PydanticObjectId`, so frontend code can use it as a plain string key without calling `.toString()`.
2. **Normalizes `created_at`** — returns a `str | None` (ISO 8601 string from `iso_utc()`) rather than a `datetime`, avoiding timezone serialization ambiguity.
3. **Flattens `source`** — instead of embedding the full `NotificationSource` object, exposes only `source_id: str | None`, which is the only source field clients need for deep-linking.
4. **Renames `type` to `kind`** — avoids shadowing Python's built-in `type` and matches the frontend convention for notification category fields.

## Field Mapping

| Document Field | Response Field | Reason |
|---|---|---|
| `str(n.id)` | `id: str` | Plain string, not ObjectId |
| `n.workspace` | `workspace_id: str` | Explicit `_id` suffix |
| `n.type` | `kind: str` | Avoid `type` shadowing |
| `n.source.id` | `source_id: str \| None` | Flattened for simplicity |
| `iso_utc(n.createdAt)` | `created_at: str \| None` | ISO string for JSON safety |

## Relationship to the Router

In practice, `NotificationResponse` is defined but the router's `GET /notifications` endpoint returns `list[dict]` (via `_to_wire()` in the service), bypassing this Pydantic model's validation. The schema is available for use in typed response annotations but is not currently wired as FastAPI's `response_model`.

This creates a documentation gap: FastAPI's OpenAPI schema for `GET /notifications` does not reference `NotificationResponse`, so the API docs do not reflect the actual response shape.

## Known Gaps

- `NotificationResponse` is not used as the `response_model` in the router, so FastAPI does not validate responses against it and the OpenAPI schema is incomplete.
- `read: bool` is present but `expires_at` from the document is not exposed — clients cannot know whether a notification is about to auto-expire.
- No `type`/`kind` enum — the schema accepts any string for `kind`, giving clients no way to enumerate valid notification categories from the schema alone.