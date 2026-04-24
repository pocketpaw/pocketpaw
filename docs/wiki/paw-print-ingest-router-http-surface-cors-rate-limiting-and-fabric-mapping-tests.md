---
{
  "title": "Paw Print Ingest Router: HTTP Surface, CORS, Rate Limiting, and Fabric Mapping Tests",
  "summary": "This test suite covers the HTTP layer of the Paw Print widget system — including owner-authenticated CRUD endpoints, CORS enforcement for the public spec and event ingest routes, payload size limits, per-customer rate limiting, and event-to-Fabric-object mapping via Jinja-style template interpolation.",
  "concepts": [
    "Paw Print ingest",
    "CORS enforcement",
    "event ingest",
    "rate limiting",
    "payload size limit",
    "token rotation",
    "Fabric object mapping",
    "template interpolation",
    "guardian rejection",
    "origin validation",
    "X-Paw-Print-Token"
  ],
  "categories": [
    "testing",
    "embeddable widgets",
    "security",
    "event processing",
    "test"
  ],
  "source_docs": [
    "d3438a407ff07fcf"
  ],
  "backlinks": null,
  "word_count": 565,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Paw Print exposes two categories of routes: owner-authed management endpoints (create, read, update, delete widgets, list events) and public-facing endpoints (serve widget spec, accept events) that are called by external websites. The public endpoints require only a widget access token and must enforce CORS, payload size, and rate limiting.

## Fixture Design

`app_with_store` creates a FastAPI app with the Paw Print router and patches `ee.paw_print.router._store` to return a fresh `PawPrintStore` backed by a temp SQLite file. The `client` fixture wraps this in a `TestClient`. This approach isolates each test from the database state.

## Widget CRUD Endpoints (`TestWidgetCRUDEndpoints`)

**Create (`test_create_widget_returns_shape`)** — `POST /paw-print/widgets` returns HTTP 201 with a body containing `pocket_id`, `access_token` (starting with `pp_tok_`), and `allowed_domains`.

**Token authentication** — `GET /paw-print/widgets/{id}` without an `X-Paw-Print-Token` header returns 401. With the correct token, it returns 200. This token-based auth model is simpler than full OAuth for external integrations that embed the widget.

**Token rotation (`test_rotate_token_changes_value`)** — `POST /paw-print/widgets/{id}/rotate-token` invalidates the old token and returns a new one. The old token value must differ from the new one. This supports incident response: if a token is leaked, the operator can rotate without deleting the widget.

**Delete (`test_delete_widget`)** — `DELETE` returns 204. Subsequent `GET` returns 404. This confirms the widget is removed, not just soft-deleted.

## Spec Serving with CORS (`TestSpecEndpoint`)

The spec endpoint serves the widget's block layout to the embedding website. CORS is enforced based on `allowed_domains`:

- **Allowed origin** — Returns spec with correct `Access-Control-Allow-Origin` header.
- **Disallowed origin** — Returns 403. A competitor cannot iframe another business's widget spec.
- **Missing origin** — Returns 403 when the widget has an allowlist. This prevents server-side fetches (which don't send an Origin header) from bypassing CORS.
- **Empty allowlist** — Allows any origin. This is the open-embed mode, useful for widgets intended for broad distribution.

## Event Ingest (`TestEventIngest`)

**Happy path** — Ingest endpoint records the event and returns an acknowledgment.

**Origin check** — Disallowed origin returns 403, same as spec endpoint. Both routes use the same domain check to prevent cross-origin event injection.

**Payload size limit** — Payloads exceeding `MAX_PAYLOAD_BYTES` are rejected with 413. This prevents denial-of-service via large payloads and protects the event store from bloat.

**Rate limiting** — After the per-customer limit is reached, further events are rejected with 429. This prevents a single user from flooding the widget's event log.

**Guardian rejection** — `test_guardian_rejection_marks_event_not_accepted` patches in a `blocker` guardian function that returns `False`. The event is stored with `accepted=False` rather than silently dropped, preserving the audit trail.

**Event mapping to Fabric** — `test_event_mapping_creates_fabric_object` patches the Fabric object creation function and verifies it is called with correctly interpolated field values. Template strings like `{{ payload.item }}` are resolved against the event payload; `{{ customer_ref }}` resolves against the event's `customer_ref` field.

## Interpolation (`TestInterpolate`)

- **Full placeholder** — A template string containing only a placeholder (`{{ payload.item }}`) returns the raw typed value, not a stringified version.
- **Mixed string** — A template with text around the placeholder (`"Order: {{ payload.item }}"`) stringifies the resolved value.
- **Missing path** — A path that doesn't exist in the context resolves to empty string in mixed mode, preventing template rendering errors from crashing ingest.

## Known Gaps

No TODOs observed. The interpolation engine's missing-path behavior (empty string) is intentional and tested, but there is no test for deeply nested paths or array indexing.
