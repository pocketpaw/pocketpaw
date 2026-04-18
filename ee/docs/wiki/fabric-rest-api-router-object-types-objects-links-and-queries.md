---
{
  "title": "Fabric REST API Router: Object Types, Objects, Links, and Queries",
  "summary": "FastAPI router exposing CRUD endpoints for the Fabric ontology layer — define object types, create/query objects, create links between objects, and retrieve store statistics. Each request creates a fresh FabricStore instance pointing at the user's local SQLite database.",
  "concepts": [
    "Fabric router",
    "APIRouter",
    "FabricStore",
    "object types API",
    "links API",
    "query API",
    "SQLite path"
  ],
  "categories": [
    "fabric",
    "ontology",
    "API",
    "REST endpoints"
  ],
  "source_docs": [
    "0a4bcebaac4d5ee7"
  ],
  "backlinks": null,
  "word_count": 340,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Fabric REST API Router: Object Types, Objects, Links, and Queries

## Purpose

This router provides the HTTP interface to the Fabric ontology store. It follows the same thin-router pattern as other PocketPaw modules — request schemas are defined inline, and all logic delegates to `FabricStore`.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/fabric/types` | List all object types |
| POST | `/fabric/types` | Define a new object type |
| POST | `/fabric/objects` | Create an object instance |
| GET | `/fabric/objects/{obj_id}` | Get a single object |
| POST | `/fabric/query` | Query objects with filters |
| POST | `/fabric/links` | Create a link between objects |
| GET | `/fabric/stats` | Get count of types, objects, links |

## Store Instantiation

The `_store()` helper creates a new `FabricStore` on every request, pointing at `~/.pocketpaw/fabric.db`. This is a deliberate simplification — since SQLite connections are lightweight and `FabricStore` uses `aiosqlite` with connection-per-operation, there's no connection pool to manage.

## Request Schemas

Defined inline in the router file (not in a separate schemas module):
- `DefineTypeRequest` — name, properties, description, icon, color
- `CreateObjectRequest` — type_id, properties, optional source tracking
- `LinkRequest` — from_id, to_id, link_type, optional properties

## Design Notes

- **No authentication**: Unlike the cloud workspace router, Fabric endpoints have no auth dependencies. This is a local-first module — it runs on the user's machine, not a shared server.
- **POST for queries**: `query_fabric` uses POST rather than GET because query parameters can be complex (nested filters, link traversal) and don't fit cleanly in URL query strings.
- **404 handling**: Only `get_object` raises HTTP 404 — other methods silently succeed or return empty results.

## Known Gaps

- No endpoint for updating or deleting object types, objects, or links — only creation and read.
- No authentication or authorization — appropriate for local use but would need guards before exposing over a network.
- The store path is hardcoded to `~/.pocketpaw/fabric.db` — not configurable via environment or settings.
