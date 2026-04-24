---
{
  "title": "FastAPI Router for Retrieval Logs and Graduation Endpoints",
  "summary": "Exposes the retrieval projection and graduation policy through a FastAPI router, providing read endpoints for recent retrievals and graduation state, plus an action endpoint that triggers a graduation scan and applies decisions. The store is cached per journal instance so the projection is not rebuilt on every request.",
  "concepts": [
    "FastAPI router",
    "retrieval endpoints",
    "graduation scan",
    "graduation apply",
    "store caching",
    "response envelopes",
    "Pydantic response models",
    "pagination placeholder",
    "journal dependency injection",
    "projection warming",
    "OpenAPI schema"
  ],
  "categories": [
    "api",
    "retrieval",
    "memory-graduation",
    "fastapi"
  ],
  "source_docs": [
    "ee/retrieval/router.py"
  ],
  "backlinks": null,
  "word_count": 443,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/retrieval/router.py` is the HTTP surface for PocketPaw's retrieval and graduation domain. It wires the journal-backed projection and policy into FastAPI endpoints that other services — dashboards, operators, and the graduation scheduler — call to inspect and manage memory tier evolution.

## Endpoint Inventory

### Read Endpoints

**`GET /retrieval/recent`** — Returns the N most recent retrieval events, with optional filters for scope, actor, pocket, and correlation ID. The response envelope is `RecentRetrievalsResponse`, which is explicitly documented as leaving room for pagination metadata without breaking callers — the `items` field exists now, and `next_cursor` can be added later.

**`GET /graduation/state`** — Lists the most recent graduation verdict for every memory. Useful for operators auditing which memories have been promoted and why.

**`GET /graduation/state/{memory_id}`** — Single-memory graduation state lookup.

### Action Endpoints

**`POST /graduation/scan`** — Triggers `scan_for_graduations` with optional overrides for window days and thresholds. Returns a flat `ScanResponse` listing every proposed decision. Decisions are *not* automatically applied — the caller decides whether to proceed.

**`POST /graduation/apply`** — Applies a list of decisions previously returned by a scan. Each decision is passed to `RetrievalJournalStore.log_graduation`, which emits `graduation.applied` onto the journal and folds it into the projection.

## Response Model Design

All response types are small Pydantic shells defined in this router file rather than in `projection.py`. This matches the convention across `ee/` routers: HTTP types (with OpenAPI schema annotations) live at the HTTP layer; pure-Python model types (dataclasses) live in the projection. The separation prevents the projection from importing Pydantic and keeps it light on hot rebuild paths.

For example, `RetrievalEntryResponse` declares every field with a `Field(description=...)` annotation so the generated OpenAPI schema is self-documenting. The projection's `RetrievalView` dataclass carries no such annotations.

## Store Caching Pattern

```python
_store_cache: dict[int, RetrievalJournalStore] = {}

def _get_store(journal: Journal) -> RetrievalJournalStore:
    key = id(journal)
    if key not in _store_cache:
        store = RetrievalJournalStore(journal)
        store.bootstrap(since_seq=0)
        _store_cache[key] = store
    return _store_cache[key]
```

The store (and by extension the projection) is cached keyed by the `id()` of the journal object. Because `Depends(get_journal)` returns the same journal singleton per process, this means one warm projection per worker process — not one rebuild per request. The pattern mirrors `ee/fleet/router.py` and `ee/fabric/router.py`.

## Graduation Threshold Defaults

The router re-exports `DEFAULT_WINDOW_DAYS`, `DEFAULT_EPISODIC_THRESHOLD`, and `DEFAULT_SEMANTIC_THRESHOLD` from `policy.py` as FastAPI `Query` defaults. This means callers that omit the query parameters get the same behaviour as the pre-refactor JSONL-based graduation scans, preserving backward compatibility.

## Known Gaps

There is no pagination on `GET /retrieval/recent` beyond a `limit` parameter. The `RecentRetrievalsResponse` envelope acknowledges this with a comment noting that `next_cursor` is reserved. Large retrieval histories cannot be efficiently paged today without a full projection rebuild from a different `since_seq`.