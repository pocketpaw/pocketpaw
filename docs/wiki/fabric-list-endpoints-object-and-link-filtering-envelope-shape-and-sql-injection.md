---
{
  "title": "Fabric List Endpoints: Object and Link Filtering, Envelope Shape, and SQL Injection Safety Tests",
  "summary": "Integration tests for the `/fabric/objects` and `/fabric/links` list endpoints added as part of the PocketDataPanel's Objects/Links sub-tab wiring. Covers store-level link filtering by type and source, HTTP envelope shape, type-based filtering, pagination parameter validation, and an explicit SQL injection safety test for the `link_type` parameter.",
  "concepts": [
    "Fabric list endpoints",
    "link filtering",
    "SQL injection",
    "parameterized binding",
    "envelope shape",
    "pagination",
    "PocketDataPanel",
    "monkeypatch",
    "aiosqlite",
    "FabricStore"
  ],
  "categories": [
    "fabric",
    "api",
    "testing",
    "security",
    "test"
  ],
  "source_docs": [
    "5069e39ae074861d"
  ],
  "backlinks": null,
  "word_count": 515,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Context: Why These Endpoints Were Added

Before this cluster of work, the Fabric router had CRUD endpoints for objects and types but no list endpoints suitable for the dashboard's sub-tab views. The `PocketDataPanel` needed paginated, filterable lists of objects and links without requiring the frontend to implement its own aggregation. These tests lock the contract between the router and the frontend so changes to the store layer do not silently break the UI.

## Fixture Design

The `client` fixture uses `monkeypatch.setattr(fabric_router_module, "_DB_PATH", test_db)` to redirect the router's lazy store initialization to a `tmp_path` database. This is the cleanest isolation pattern for module-level singletons: no mocking of the store itself, just redirecting where it reads its data from.

## test_store_list_links_filters_by_type

Three customers are linked with two relationship types: `reports_to` (2 links) and `mentors` (1 link). The test verifies:

- `list_links(link_type="reports_to")` returns exactly 2 links, all with the correct `link_type`.
- `list_links(from_id=o1.id)` returns the 2 links originating from `o1` regardless of link type.

This dual-filter test is important because the frontend sub-tab toggle switches between "by type" and "by source" views — both code paths must work correctly.

## test_store_list_links_binds_params_no_injection

This is an explicit SQL injection safety test. The `link_type` parameter is user-controlled (passed via query string), and naively interpolating it into SQL would allow `link_type=" reports_to'; DROP TABLE fabric_links; --"` to corrupt the database.

The test passes exactly that payload as the `link_type` and verifies:

1. The result is empty (the injection string matches no rows).
2. `list_links()` with no filter still returns the legitimate link (the table was not dropped).

The comment explains why the risk is bounded but still worth testing:

> "SQLite won't execute multi-statement queries via aiosqlite's execute(), so the SQL-injection vector is inherently weaker... We still prove by construction that the filter is bound."

Parameterized binding is verified by construction: if the value were concatenated, the trailing `DROP TABLE` would either error or silently fail and the link count would be wrong.

## test_route_list_objects_returns_envelope

The list endpoint must return an envelope shape — a wrapper object with `items` and `total` fields rather than a raw array. This allows the frontend to know the total count for pagination controls without a separate count request.

The test creates a type and an object via POST, then GETs `/api/v1/fabric/objects` and checks that the response body has the correct envelope keys.

## test_route_list_objects_filter_by_type

GET `/api/v1/fabric/objects?type_name=Task` returns only Task objects, not objects of other types. This is the primary filter the PocketDataPanel uses to populate the Objects sub-tab when a user selects a specific type.

## test_route_list_links_returns_envelope

Same envelope contract test for the links endpoint. Links must also be returned in a `{items: [...], total: N}` wrapper.

## test_route_list_links_rejects_bad_limit

Passing `limit=-1` or `limit=0` must return 422 (validation error), not 500. This prevents the store from executing `SELECT ... LIMIT -1` which in SQLite returns all rows, bypassing the intended pagination cap.

## Known Gaps

No test covers the `offset` parameter for the HTTP endpoints (pagination beyond the first page). The store-level pagination is tested in `test_ee_fabric.py` but the router-level offset behavior is not verified here.