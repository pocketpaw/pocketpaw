---
{
  "title": "FastAPI Router for Widget Interactions, Graduation, and Co-occurrence",
  "summary": "Exposes the widget journal projection through REST endpoints for reading widget usage stats, graduation state, and co-occurrence suggestions, plus write endpoints for tracking interactions and recording accept/dismiss decisions on suggested widget pairings. The POST /widgets/track endpoint closes a silent data-loss bug where every dashboard interaction 404'd before this change.",
  "concepts": [
    "FastAPI router",
    "widget tracking",
    "silent 404 bug fix",
    "co-occurrence accept/dismiss",
    "store caching",
    "scope fallback",
    "SuggestedWidgetsFeed",
    "journal sequence ack",
    "widget graduation endpoint",
    "Pydantic response models"
  ],
  "categories": [
    "api",
    "widget-system",
    "co-occurrence",
    "fastapi"
  ],
  "source_docs": [
    "ee/widget/router.py"
  ],
  "backlinks": null,
  "word_count": 431,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/widget/router.py` is the HTTP surface for the widget domain. It serves three categories of reads from the journal-backed projection and two categories of writes that land new events on the journal.

## The Silent 404 Bug

One of the most operationally significant changes in this file is `POST /widgets/track`. The `SuggestedWidgetsFeed` UI (paw-enterprise PR #74) had been posting widget interactions to this route since it shipped, but the route did not exist until this PR. Every interaction silently 404'd and was discarded. No error was surfaced to the UI because dashboard event tracking typically fires-and-forgets.

The fix validates the payload, emits `widget.interaction.recorded` via `WidgetJournalStore.log_widget_interaction_with_seq`, and returns the journal sequence number on the ack. The seq allows the UI to optionally pin a cursor for polling consistency.

## Endpoint Inventory

### Read Endpoints

**`GET /widgets/usage`** — Returns per-`(widget, surface)` usage stats from the projection. Supports filtering by surface.

**`GET /widgets/graduation`** — Lists the most recent graduation verdict per widget.

**`GET /widgets/cooccurrence`** — Lists co-occurring widget pairs above threshold, with accepted/dismissed flags so the feed can filter already-actioned suggestions.

### Write Endpoints

**`POST /widgets/track`** — Records one user interaction with a widget. Validates the `WidgetInteractionRequest` payload (widget name, surface, action type, optional query text, optional pocket ID). Falls back to `["org:*"]` scope when the actor carries no `scope_context` — this covers the anonymous-session path from the dashboard UI.

**`POST /widgets/cooccurrence/accept`** — Records that an operator accepted a widget pairing suggestion. Emits `widget.cooccurrence.accepted`.

**`POST /widgets/cooccurrence/dismiss`** — Records that an operator dismissed a suggestion. Emits `widget.cooccurrence.dismissed`.

The two cooccurrence endpoints were added in Cluster B Sub-PR #2. Before this change, the accept/dismiss buttons in paw-enterprise were client-local only — dismissals were forgotten on page reload.

## Response Schema

All response models are defined in this file as Pydantic `BaseModel` subclasses with `Field(description=...)` annotations for OpenAPI. The `WidgetUsageResponse` and `CooccurrenceResponse` use a list envelope that leaves room for pagination metadata. Pagination itself is not yet implemented.

## Scope Fallback Logic

```python
scope = actor.scope_context or ["org:*"]
```

The fallback to `["org:*"]` is deliberate. The SuggestedWidgetsFeed in the enterprise dashboard posts without a user-specific scope because widget usage tracking is org-level analytics, not per-user data. A stricter fallback (e.g., rejecting scopeless requests) would break every anonymous dashboard session.

## Store Caching

The same store-per-journal caching pattern as `ee/retrieval/router.py` is used: `_get_store(journal)` caches the warmed `WidgetJournalStore` by `id(journal)`. First call bootstraps the projection; subsequent writes apply incrementally.

## Known Gaps

Pagination is not implemented on the usage or co-occurrence endpoints. The `WidgetUsageResponse` envelope has a `total` field but no cursor or offset parameters on the query.