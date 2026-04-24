---
{
  "title": "Widget Track Endpoint Tests: Interaction Recording, Validation, Scope Fallback, and Projection Integration",
  "summary": "Tests for `POST /widgets/track`, the writer endpoint that records widget interactions from the SuggestedWidgetsFeed UI. Covers the full happy-path event emission, Pydantic validation of required fields, metadata defaulting, correlation-id propagation, sequential seq increments, the `org:*` scope fallback for anonymous actors, and an end-to-end proof that three POSTs reflect correctly in the `GET /widgets/usage` projection.",
  "concepts": [
    "widget track endpoint",
    "POST /widgets/track",
    "interaction recording",
    "scope fallback",
    "correlation_id",
    "downstream projection",
    "WidgetJournalStore",
    "FastAPI dependency_overrides",
    "seq monotonicity",
    "Pydantic validation"
  ],
  "categories": [
    "testing",
    "enterprise features",
    "widget system",
    "API endpoints",
    "test"
  ],
  "source_docs": [
    "e6d9b7445acdd101"
  ],
  "backlinks": null,
  "word_count": 444,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/ee/test_widget_track_endpoint.py` was created in `feat/widget-track-endpoint` to close the integration loop opened by issue #955 (widget journal projection) and paw-enterprise #74 (SuggestedWidgetsFeed UI). Before this endpoint existed, the UI had been POSTing to `/widgets/track` for weeks and receiving 404s — all interactions were silently dropped. These tests lock the writer contract so a future refactor cannot quietly regress it.

## Fixture Design

The `app` fixture opens a single journal instance and installs it via `dependency_overrides`:

```python
@pytest.fixture
def app(journal_path):
    a = FastAPI()
    a.include_router(router)
    _journal = open_journal(journal_path)
    a.dependency_overrides[get_journal] = lambda: _journal
    return a
```

A single instance is intentional — the router's store cache keys off `id(journal)`. If a fresh journal were opened on every request, the cache would create a new store per request, and the projection would never accumulate state, causing the downstream projection tests to fail silently.

## Test Class Breakdown

### TestHappyPath
Posts a valid payload and asserts:
- Status 200 with `{ok: true, event_id: "<uuid>", seq: 0}`.
- The journal contains exactly one `widget.interaction.recorded` event.
- Every request field (widget_name, actor.kind, actor.id, scope, action_type, surface, pocket_id, metadata) maps verbatim to the event payload.

### TestValidation
Missing `widget_name`, empty `widget_name`, unknown `actor.kind`, empty `actor.id`, and empty `action_type` all produce 422. These are validated by Pydantic at the FastAPI layer before any business logic runs, so no journal writes occur on invalid input.

### TestMetadataDefault
The UI often omits `metadata` or sends `null`. Both cases must default to `{}` in the stored event so downstream consumers can safely do `ev.payload["metadata"]["key"]` without a KeyError.

### TestCorrelationId
The UI generates `wi_<uuid>` strings as correlation ids. The test verifies that a bare UUID string passes validation and that the same value appears in the journal event:

```python
def test_correlation_id_propagates_to_event(client, app):
    cid = str(uuid4())
    res = client.post("/widgets/track", json=_valid_payload(correlation_id=cid))
    ev = journal.query(action=ACTION_WIDGET_INTERACTION_RECORDED)[0]
    assert ev.payload["correlation_id"] == cid
```

### TestSequentialWrites
Three POSTs yield three events with distinct `event_id` values and strictly increasing `seq` numbers. This confirms the journal's append-only monotonicity.

### TestScopeFallback
The UI's anonymous-actor path sends `scope_context=[]`. Because the journal refuses empty scope, the router must substitute `["org:*"]` before appending. Tests cover both `scope_context=[]` and an absent `scope_context` key.

### TestDownstreamProjection
Three `open` interactions for the same widget, then `GET /widgets/usage` — the row for `metrics_chart` must show `count=3` and `promoting_count=3`. Mixed actions (open + view) split the counters correctly. This is the end-to-end closure proof: the HTTP writer feeds the journal, the projection folds those events, and the reader endpoint reflects the accumulated state.

## Known Gaps

No test covers the case where `journal.append()` raises mid-request (e.g., disk full). The endpoint would return a 500 without an ack, but the client-side retry behaviour is untested.