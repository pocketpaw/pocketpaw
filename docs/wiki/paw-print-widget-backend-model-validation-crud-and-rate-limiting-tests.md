---
{
  "title": "Paw Print Widget Backend: Model Validation, CRUD, and Rate Limiting Tests",
  "summary": "This test suite covers the backend layer of PocketPaw's Paw Print embeddable widget system — validating Pydantic model constraints (block caps, domain normalization, rate limits, token format), SQLite-backed CRUD operations, event persistence, and the dual rate-limit primitives (overall and per-customer) that protect the public ingest endpoint.",
  "concepts": [
    "Paw Print",
    "PawPrintWidget",
    "PawPrintSpec",
    "PawPrintStore",
    "block caps",
    "domain normalization",
    "rate limiting",
    "token rotation",
    "event store",
    "CRUD",
    "SQLite fixture"
  ],
  "categories": [
    "testing",
    "embeddable widgets",
    "rate limiting",
    "data validation",
    "test"
  ],
  "source_docs": [
    "1659ed1c68083b05"
  ],
  "backlinks": null,
  "word_count": 507,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Paw Print is PocketPaw's embeddable widget system that allows businesses to embed an AI-powered interactive display (menus, booking flows, product cards) on their websites. The backend stores widget configuration, tracks inbound events, and enforces rate limits. This test file covers the model validation layer and the SQLite store.

## Model Validation

**Block caps (`TestBlockCaps`)** — A `PawPrintSpec` can have at most `MAX_BLOCKS_PER_SPEC` blocks, and a list block can have at most `MAX_ITEMS_PER_LIST` items. These caps exist to prevent widgets from consuming excessive rendering resources in the browser. `test_list_block_accepts_up_to_the_cap` confirms the boundary is inclusive; `test_list_block_rejects_past_the_cap` confirms the cap is enforced with a descriptive error message.

**Domain normalization (`TestWidgetValidation`)** — `allowed_domains` are lowercased and deduplicated on creation. This prevents a configuration bug where `BrewCo.com` and `brewco.com` are treated as different domains, causing CORS rejections for half the requests. The cap on `MAX_DOMAINS_PER_WIDGET` prevents abuse where a bad actor lists thousands of domains.

**Rate limit validation** — `rate_limit_per_min` and `per_customer_limit_per_min` must be positive integers. A zero or negative rate limit would either allow unlimited traffic or produce confusing behavior in the rate-limit check.

**Access token generation** — Every widget gets an auto-generated access token prefixed with `pp_tok_`. The prefix makes tokens identifiable in logs and makes accidental leakage of the wrong credential type recognizable. The length check (>20 chars beyond the prefix) ensures the token has sufficient entropy.

**Event type validation (`TestEventValidation`)** — Empty event types (or whitespace-only strings) are rejected. Event types are stripped of surrounding whitespace, preventing `" order_click "` from being stored as a different event than `"order_click"`.

## Store CRUD (`TestWidgetCRUD`)

The `store` fixture creates a fresh `PawPrintStore` backed by a temp SQLite file per test. Tests verify:

- **Create and fetch** — A created widget can be retrieved by ID with all fields intact.
- **List filtering** — `list` filters by both `pocket_id` and `owner`. A user cannot see another user's widgets even within the same pocket.
- **Update** — `update_spec` replaces the blocks in the spec. The store returns the updated widget.
- **Token rotation** — `rotate_token` generates a new access token and invalidates the old one. The old token must not work after rotation.
- **Delete** — `delete_widget` returns `True` on first call, `False` on second (idempotent delete). After deletion, `get` returns `None`.
- **Update missing widget** — `update` on a non-existent widget ID returns `None`, not an exception.

## Event Store (`TestEventStore`)

Events are stored in insertion order and returned newest-first. `test_count_events_since_respects_window` confirms the time-window query for rate limit checks counts only events within the window.

**Rate limiting (`test_within_rate_limit_enforces_overall_and_per_customer`)** — The store provides a `within_rate_limit` check that enforces two limits simultaneously: an overall per-minute limit for the widget and a per-customer limit. The per-customer limit prevents a single user from consuming the entire widget quota. `test_within_rate_limit_respects_overall_ceiling` confirms the overall limit fires even when no single customer has hit their personal limit.

## Known Gaps

No TODOs observed. The rate-limit tests cover both limit dimensions, but do not test concurrent access (thread safety of the SQLite store under parallel event ingest).
