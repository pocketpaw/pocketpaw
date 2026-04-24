---
{
  "title": "Common API Response Schemas — Shared Base Types",
  "summary": "The common schemas define PocketPaw's shared response envelope types: a base class with ORM compatibility, a standard error shape, a generic paginated list, and two convenience success models. These types appear across every domain in the v1 API.",
  "concepts": [
    "APIResponse",
    "ErrorResponse",
    "PaginatedResponse",
    "OkResponse",
    "StatusResponse",
    "from_attributes",
    "ORM compatibility",
    "generic types",
    "error codes",
    "pagination"
  ],
  "categories": [
    "schemas",
    "API",
    "architecture"
  ],
  "source_docs": [
    "a0eb4bf7d01dd0ab"
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

Every well-designed API has a handful of universal response shapes that appear everywhere: a success indicator, an error envelope, and a paginated list. PocketPaw's `common.py` defines these building blocks so all routers speak the same structural language.

## `APIResponse` — Base Class

```python
class APIResponse(BaseModel):
    model_config = {"from_attributes": True}
```

The `from_attributes=True` config enables Pydantic to populate the model from ORM objects (SQLAlchemy, Tortoise) by reading attributes rather than requiring a dict. This means router handlers can pass database model instances directly to response models without manually converting them to dicts. Without this setting, `return SomeResponse(field=orm_obj.field)` works, but `return SomeResponse.model_validate(orm_obj)` would fail.

All other response types in `common.py` inherit from `APIResponse`, so they all get this ORM compatibility for free.

## `ErrorResponse`

```python
class ErrorResponse(APIResponse):
    detail: str
    code: str | None = None
```

The optional `code` field carries a machine-readable error identifier (e.g., `"memory_backend_unavailable"`, `"token_expired"`) alongside the human-readable `detail` string. This allows clients to handle specific error cases programmatically without parsing the `detail` message text. `code` is `None` for generic errors where no specific code applies.

## `PaginatedResponse`

```python
class PaginatedResponse(APIResponse, Generic[T]):
    items: list[T]
    total: int
    offset: int = 0
    limit: int = 50
```

A generic paginated list that works with any item type. The `total` count is the number of items matching the query (not just the current page), which allows clients to calculate page counts and render pagination controls. Default `offset=0` and `limit=50` reflect cursor-based pagination: start at the beginning, return 50 items.

Using `Generic[T]` means the OpenAPI schema generation can produce typed list responses (e.g., `PaginatedResponse[MemoryEntry]`) rather than a generic `items: list[object]`.

## `OkResponse`

```python
class OkResponse(APIResponse):
    ok: bool = True
```

A minimal success indicator. Used for delete operations and other actions where there is nothing meaningful to return beyond confirmation of success. The `ok=True` default means the model can be returned as `return OkResponse()` without any arguments.

## `StatusResponse`

```python
class StatusResponse(APIResponse):
    status: str = "ok"
```

Similar to `OkResponse` but returns a string status rather than a boolean. Used in contexts where the status might carry additional information (e.g., `status="reloading"`) without requiring a new response type.

## Design Rationale

Defining these shared types in a single module prevents drift. Without a common base, individual routers might define their own incompatible success shapes (`{"success": true}`, `{"result": "ok"}`, `{"status": "success"}`). Using these shared types ensures the API has a consistent response language, making client code simpler and API documentation more coherent.

## Known Gaps

- `PaginatedResponse` uses offset-based pagination. For large, frequently-updated datasets, cursor-based pagination (returning a `next_cursor` token) is more reliable because insertions or deletions between pages do not cause rows to be skipped or duplicated.
- `ErrorResponse.code` has no defined vocabulary. There is no enum or registry of valid error codes, making it easy for routers to emit inconsistent codes for the same error conditions.
