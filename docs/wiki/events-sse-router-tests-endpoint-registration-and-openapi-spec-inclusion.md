---
{
  "title": "Events SSE Router Tests: Endpoint Registration and OpenAPI Spec Inclusion",
  "summary": "Tests that PocketPaw's server-sent events (SSE) endpoint `/api/v1/events/stream` is correctly registered in the events router, present in the v1 router registry, accessible on the dashboard app, and included in the OpenAPI specification. These structural checks prevent the events channel from being silently dropped during refactors.",
  "concepts": [
    "SSE endpoint",
    "events router",
    "server-sent events",
    "OpenAPI spec",
    "_V1_ROUTERS",
    "dashboard app",
    "route registration",
    "FastAPI tags",
    "real-time events",
    "localhost guard"
  ],
  "categories": [
    "testing",
    "real-time streaming",
    "API configuration",
    "event system",
    "test"
  ],
  "source_docs": [
    "4cdbd18cc21b4282"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_events_sse.py` validates the structural registration of PocketPaw's real-time events SSE endpoint. The events router delivers system-level notifications (agent state changes, tool execution updates, background job progress) to dashboard consumers over a persistent HTTP connection using the Server-Sent Events protocol.

## Why Structural Registration Tests Exist

FastAPI routers are only active if they are both defined and mounted. A router that defines `/events/stream` but is not included in `_V1_ROUTERS` produces 404s for every client that tries to connect. Similarly, a router that is mounted but not tagged correctly will not appear in the OpenAPI spec, making it invisible to SDK generators and API documentation consumers.

These tests are deliberately shallow — they do not test the stream content — because the purpose is to catch registration regressions, not streaming logic regressions.

## Router Endpoint Presence

`test_router_has_stream_endpoint` inspects `router.routes` directly for a route with path `/events/stream`. This asserts that the route is defined within the events router object before any app-level mounting occurs.

## Router Tagging

`test_router_tags` verifies the router declares `"Events"` in its tag list. OpenAPI tags group endpoints in the generated documentation and in API client generators. Without the correct tag, auto-generated clients may place events-related methods in the wrong namespace, and documentation becomes disorganized.

## V1 Registry Inclusion

`test_router_registered_in_v1` checks that `pocketpaw.api.v1.events` appears in `_V1_ROUTERS`. This complements the broader registry test in `test_api_cors.py` by verifying the events module specifically — given that the events endpoint is a later addition and more likely to be missed in registry updates.

## Dashboard App Accessibility

`test_stream_endpoint_exists_on_dashboard` instantiates the full dashboard `app` and inspects its route table for a path containing `/api/v1/events/stream`. The test patches `pocketpaw.dashboard_auth._is_genuine_localhost` to `True` to bypass the localhost-only guard that normally prevents the dashboard from loading in test environments.

**Failure it prevents:** The events router might be correctly registered in `_V1_ROUTERS` but still omitted from the dashboard's own `mount_v1_routers` call, producing 404s specifically on the dashboard app while the standalone API server works correctly.

## OpenAPI Specification Inclusion

`test_openapi_includes_events` fetches the live OpenAPI schema from `/api/v1/openapi.json` using a `TestClient` and asserts that at least one path in `paths` contains `"events/stream"`. This is the strongest check — it proves the endpoint survives the full FastAPI app startup, route scanning, and schema generation pipeline.

**Failure scenario:** If the events router is mounted with an incorrect prefix, routes appear under the wrong path and are absent from the expected path in the schema.

## Known Gaps

No TODO or FIXME markers are present. The tests do not exercise the SSE stream body content, connection lifecycle, or reconnection behavior. A follow-on test suite focused on streaming correctness (similar to the chat stream tests) would complement these structural checks.