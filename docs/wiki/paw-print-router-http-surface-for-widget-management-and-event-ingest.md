---
{
  "title": "Paw Print Router — HTTP Surface for Widget Management and Event Ingest",
  "summary": "Provides the FastAPI routes for the Paw Print widget layer: public CORS-gated spec serving, owner-authenticated widget CRUD, and a multi-layered event ingest endpoint that enforces domain validation, rate limiting, Guardian screening, and Fabric object mapping before persisting customer interactions.",
  "concepts": [
    "CORS per-widget enforcement",
    "_origin_allowed",
    "rate limiting",
    "Guardian screening",
    "_pass_through_guardian",
    "_interpolate template",
    "_require_owner_token",
    "event ingest pipeline",
    "Fabric object mapping",
    "MAX_PAYLOAD_BYTES",
    "X-Widget-Token"
  ],
  "categories": [
    "paw print",
    "REST API",
    "security",
    "enterprise edition"
  ],
  "source_docs": [
    "562612bf0e3eb39c"
  ],
  "backlinks": null,
  "word_count": 511,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The Paw Print router is the HTTP boundary between the customer's browser (via widget.js) and the backend decision loop. It handles two distinct caller types: the widget.js bundle (unauthenticated, public, CORS-restricted) and the widget owner (authenticated via access token, managing widget configuration). This dual-caller model drives the layered security design.

## Public Spec Endpoint

The widget.js bundle fetches the spec on load via `GET /paw-print/widgets/{widget_id}/spec`. This endpoint is intentionally unauthenticated — the widget token is a read key, not a write key, and widget.js runs in untrusted browser contexts. CORS enforcement is per-widget: the handler reads `widget.allowed_domains` and sets `Access-Control-Allow-Origin` to the specific matching origin, not a wildcard. `_origin_allowed()` performs the domain matching.

## _origin_allowed — Domain Validation

This function matches the inbound `Origin` header against the widget's `allowed_domains` list. Exact string matching is used (no wildcard subdomains). If the origin does not match, the spec and event ingest endpoints return 403. This prevents a widget configured for `app.example.com` from being embedded on an attacker's page.

## Event Ingest Pipeline

The event ingest endpoint `POST /paw-print/widgets/{widget_id}/events` applies security checks in strict order:

1. **Widget existence** — 404 if widget not found.
2. **Origin enforcement** — 403 if origin not in allowed_domains.
3. **Rate limit check** — calls `store.within_rate_limit()` for both the overall widget rate and the per-customer rate. Returns 429 on breach.
4. **Payload size check** — rejects payloads over `MAX_PAYLOAD_BYTES` (4KB).
5. **Guardian screening** — `_pass_through_guardian()` calls the Guardian security layer for content screening. This is best-effort: if Guardian is absent or raises, the event is allowed through with a warning log. Guardian is an enhancement, not a gate.
6. **Fabric mapping** — `_interpolate()` resolves `{{ payload.field }}` placeholders in the event mapping's `field_map` against the event payload, then creates or updates the target Fabric object.
7. **Event persistence** — the raw event is recorded in the store for replay and audit.

## _interpolate and _lookup — Template Placeholders

`_interpolate(template, context)` resolves `{{ a.b.c }}` dot-path placeholders against the event payload using `_PLACEHOLDER_RE`. `_lookup(path, context)` traverses nested dicts with dot notation. This allows event mappings to be configured as templates (`"customer_id": "{{ payload.customer_id }}"`), making the Fabric mapping configurable without code changes.

## _require_owner_token — Write Protection

Widget CRUD endpoints (create, update, delete) call `_require_owner_token(widget, header_token)` which compares the `X-Widget-Token` header against the stored `access_token` using a constant-time comparison to prevent timing attacks.

## _pass_through_guardian — Tolerant Security Screen

The Guardian screen is wrapped in a try/except that returns `True` (allow) on any failure. The failure mode is permissive because: (a) Guardian may not be installed in all deployments, and (b) blocking customer interactions because a security service is temporarily down would break customer-facing UX in a highly visible way.

## Known Gaps

- `_store()` uses a late import (`from ee.api import get_paw_print_store`) to avoid circular imports — this is a HACK pattern consistent with the Instinct router but should be resolved with dependency injection.
- Fabric object creation on event ingest is fire-and-forget with no retry on failure; a transient Fabric write error silently drops the mapping.