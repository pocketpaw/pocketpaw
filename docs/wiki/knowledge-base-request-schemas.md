---
{
  "title": "Knowledge Base Request Schemas",
  "summary": "Pydantic request models for the knowledge base REST API, covering search, text ingestion, URL ingestion, and lint operations. These schemas enforce input validation at the FastAPI boundary before any request reaches the kb Go binary.",
  "concepts": [
    "Pydantic",
    "request schemas",
    "input validation",
    "SearchRequest",
    "IngestTextRequest",
    "IngestUrlRequest",
    "LintRequest",
    "scope override",
    "knowledge base",
    "FastAPI"
  ],
  "categories": [
    "knowledge-base",
    "API schemas",
    "validation"
  ],
  "source_docs": [
    "2b53f55273b673d8"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`schemas.py` defines the four Pydantic request models used by the KB router. Separating schemas into their own module keeps the router file focused on routing logic and allows the schemas to be imported independently by tests and other consumers without pulling in FastAPI dependencies.

## SearchRequest

```python
class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    scope: str | None = None
    limit: int = Field(default=10, ge=1, le=100)
```

The `min_length=1` constraint on `query` prevents empty-string searches from reaching the kb binary. The `limit` is bounded at 100 to prevent clients from accidentally requesting very large result sets that could cause memory pressure in the binary's in-process BM25 scoring. The `scope` override is optional — when absent, the router defaults to the workspace scope.

## IngestTextRequest

```python
class IngestTextRequest(BaseModel):
    text: str = Field(min_length=1)
    source: str = "manual"
    scope: str | None = None
```

The `source` field carries provenance metadata — it tells the KB index where the content came from (`"confluence"`, `"github"`, `"manual"`, etc.). This surfaces in the article metadata returned by list/search endpoints and allows the UI to show provenance badges. Defaulting to `"manual"` covers the common case of a user pasting text directly.

## IngestUrlRequest

```python
class IngestUrlRequest(BaseModel):
    url: str = Field(min_length=1)
    scope: str | None = None
```

URL ingestion validates only that the string is non-empty at the schema level. The router's `_extract_url()` call is responsible for actual URL validation and fetching. This separation keeps the schema simple and avoids the complexity of embedding HTTP validation rules in Pydantic (which would need to handle private IP blocklists, redirect policy, etc.).

## LintRequest

```python
class LintRequest(BaseModel):
    scope: str | None = None
```

Lint takes no content — it operates entirely on what is already in the KB index for the given scope. The optional `scope` override follows the same pattern as the other request types.

## Design Rationale

All four schemas share the `scope: str | None = None` field. Rather than duplicating this with inheritance, the models are kept flat — each is small enough that a base class would add indirection without meaningful benefit. The simplicity also makes it easy to evolve schemas independently as KB capabilities expand (e.g., adding `filters` to `SearchRequest` without touching ingest schemas).

## Known Gaps

- `IngestUrlRequest.url` has no URL format validation. A malformed URL will pass schema validation and only fail when the router attempts to fetch it.
- `SearchRequest` has no `filters` or `facets` fields — result filtering (by source, by date range, by agent) must be done by the client on the returned list.
- No response schemas are defined here; all endpoints return untyped `dict` responses. Adding response models would enable automatic OpenAPI response documentation.