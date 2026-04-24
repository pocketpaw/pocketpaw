---
{
  "title": "Fabric Store: Object Type CRUD, Object Lifecycle, Link Traversal, and Query Tests",
  "summary": "Unit tests for the enterprise `FabricStore` — PocketPaw's ontology-based business object store backed by SQLite. Covers defining and querying object types, creating and updating objects with source tracking, linking objects and traversing relationships, filtering queries, pagination, and cascade deletion.",
  "concepts": [
    "FabricStore",
    "ObjectType",
    "FabricObject",
    "link traversal",
    "cascade deletion",
    "source tracking",
    "FabricQuery",
    "pagination",
    "ontology",
    "graph database"
  ],
  "categories": [
    "fabric",
    "enterprise",
    "testing",
    "store",
    "test"
  ],
  "source_docs": [
    "81373d45d18f0f6c"
  ],
  "backlinks": null,
  "word_count": 554,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## What FabricStore Is

The `FabricStore` is an ontology-aware object database: you define types with schemas, create typed objects, and link them in a graph. It is the "memory of the business" — where customer records, orders, invoices, products, and any other domain entities live as first-class objects that agents can query and reason over.

## TestObjectTypes

**`test_define_and_get`** — creates a `Customer` type with three properties (`name` required, `email`, `revenue`) and verifies the round trip. The ID prefix `ot-` is checked to ensure the ID generation contract is stable.

**`test_get_by_name`** — type lookup by name is case-insensitive: defining `"Order"` and querying `"order"` returns the same type. This prevents case sensitivity bugs from breaking connector-to-Fabric ingestion pipelines where the type name comes from external data.

**`test_list_types`** — after creating two types, `list_types()` returns exactly two. This is a simple completeness check that the list query does not filter or paginate unexpectedly.

**`test_remove_cascades`** — this is the most important type test. When a type is deleted, all objects of that type and all links involving those objects are also deleted. Without cascade deletion, removing a type would leave orphaned objects that could never be queried (their type no longer exists) but would still consume storage and could corrupt stats counts.

## TestObjects

**`test_create_and_get`** — object creation returns an `id` with `obj-` prefix and a `type_name` field populated from the type definition. The `get_object()` round trip verifies persistence.

**`test_update`** — `update_object()` merges the new properties with existing ones (patch semantics, not replace). Updating `revenue` from 50000 to 75000 leaves `name` unchanged. This is the correct behavior for incremental sync from external connectors.

**`test_source_tracking`** — objects can carry `source_id` and `source_connector` metadata indicating which external system they came from. This test verifies these fields survive the store round trip, enabling deduplication and provenance tracking in connector ingestion pipelines.

**`test_remove`** — deleting an object removes it from the store. A subsequent `get_object()` returns `None`. Links involving the deleted object should also be cleaned up (implied by cascade behavior, though not explicitly tested in this suite).

## TestLinks

**`test_link_and_traverse`** — links two objects with a typed relationship (`"purchased"`) and verifies traversal via `get_linked_objects(from_id, "purchased")`. This is the graph API that agents use to answer questions like "show me all orders placed by this customer."

**`test_unlink`** — removes a link and verifies that traversal no longer returns the previously linked object. Without `unlink`, the graph would accumulate stale edges.

## TestQuery

**`test_by_type_name`** — `FabricQuery(type_name="Product")` returns only `Product` objects, not objects of other types. Mixed-type queries are the most common agent operation.

**`test_by_linked`** — `FabricQuery(linked_to="obj-x")` returns objects linked to a specific object. This enables "show me everything related to this customer" without a full graph traversal.

**`test_pagination`** — `FabricQuery(limit=2, offset=0)` returns two results from a three-object set. A second page at `offset=2` returns the remaining one. Without pagination, large object stores would return unbounded result sets to the agent.

**`test_stats`** — `store.stats()` returns counts of `objects`, `links`, and `types`. This is used by the dashboard's overview panel and by automation evaluators that need to know if a store is empty before evaluating rules.

## Known Gaps

No test covers the behavior of `get_linked_objects` when the linked object has been deleted. The cascade deletion test removes the type (which cascades to objects), but does not verify that link traversal handles deleted objects gracefully.