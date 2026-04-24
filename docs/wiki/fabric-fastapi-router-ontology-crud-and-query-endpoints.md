---
{
  "title": "Fabric FastAPI Router — Ontology CRUD and Query Endpoints",
  "summary": "The Fabric router exposes HTTP endpoints for managing the ontology layer: defining object types, creating and querying objects, creating and listing links, and retrieving stats. It uses the legacy SQLite `FabricStore` for all persistence and was updated in Cluster C / PR3 to add `GET /fabric/objects` and `GET /fabric/links` so the PocketDataPanel's Objects and Links sub-tabs render real data.",
  "concepts": [
    "Fabric router",
    "FabricStore",
    "SQL injection safety",
    "ObjectsListResponse",
    "LinksListResponse",
    "FabricQuery",
    "parameterized query",
    "PocketDataPanel",
    "ontology CRUD",
    "fabric stats"
  ],
  "categories": [
    "fabric",
    "HTTP API",
    "FastAPI",
    "ontology",
    "CRUD"
  ],
  "source_docs": [
    "6a6d1b8307734da3"
  ],
  "backlinks": null,
  "word_count": 394,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The Fabric router is the HTTP face of the ontology layer. It mounts under `tags=["Fabric"]` without an explicit router prefix, so routes use their full paths: `/fabric/types`, `/fabric/objects`, `/fabric/links`, etc. The `_store()` factory function creates a new `FabricStore` instance pointing at `~/.pocketpaw/fabric.db` on each request — this is safe because `FabricStore` is stateless across calls (connection state is managed per operation).

## SQL Injection Safety

The `list_objects` endpoint comment calls out a specific security property: type filters go through `FabricQuery`, which the store binds as parameterized query values — user input is never concatenated into SQL strings. The same pattern applies to `list_links` where `from_id`, `to_id`, and `link_type` are all bound parameters.

## Endpoint Groups

**Types** (`/fabric/types`): `GET` lists all defined types; `POST` defines a new one. Types are the schema layer — once defined they rarely change.

**Objects** (`/fabric/objects`): `GET` lists objects with optional type filter, pagination, and returns `ObjectsListResponse(objects, total)`. `POST` creates a new instance. `GET /{obj_id}` fetches a single object by ID with a 404 if absent. `POST /fabric/query` accepts a full `FabricQuery` body for richer filtering (property filters, linked-to traversal).

**Links** (`/fabric/links`): `GET` lists links with optional endpoint and type filters, returning `LinksListResponse(links, total)`. `POST` creates a new directional link.

**Stats** (`/fabric/stats`): Returns a summary of the store state — object counts, type counts, link counts. Used by the UI's status panel.

## Response Models

`ObjectsListResponse` and `LinksListResponse` wrap the list + total count in a structured response rather than returning a bare array. This makes it unambiguous whether pagination is in play and avoids the common API anti-pattern of returning `{"data": [...], "total": N}` without documenting the shape.

## Update (Cluster C / PR3)

The `GET /fabric/objects` and `GET /fabric/links` endpoints were added specifically to back the Objects and Links sub-tabs in `PocketDataPanel`. Before this change, the sub-tabs rendered hardcoded mock data from "Brew & Co.". The addition of these endpoints required the companion `list_links()` method in `FabricStore`.

## Known Gaps

- The `_store()` factory creates a new `FabricStore` on every request, including the schema check. For high-frequency endpoints this is wasteful; a request-scoped or application-scoped singleton would be more efficient.
- The router uses the legacy `FabricStore`, not the new `FabricJournalStore`. Object creation through this router does not emit journal events and does not support scope filtering. Migration to the journal path is a future slice.