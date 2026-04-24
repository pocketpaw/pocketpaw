---
{
  "title": "Files API Router: Unified Listing and v2 Tree/Browse Endpoints",
  "summary": "Defines the FastAPI router for the EE `/files` endpoints, including the legacy `GET /files` unified listing endpoint and the v2 tree/browse API built on `ProviderRegistry`. The router factory `build_router` composes the registry, ABAC rules, and request-context factory into a single mountable router.",
  "concepts": [
    "build_router",
    "ProviderRegistry",
    "GET /files",
    "FilesPanel",
    "FolderNode tree",
    "browse endpoint",
    "request_context_factory",
    "FilesError handler",
    "EE license gating",
    "UnifiedFilesService",
    "APIRouter",
    "ABAC"
  ],
  "categories": [
    "files",
    "api",
    "routing",
    "cloud"
  ],
  "source_docs": [
    "da0b2950ceca352b"
  ],
  "backlinks": null,
  "word_count": 420,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee.cloud.files.router` is the HTTP boundary of the files subsystem. It exposes two generations of the files API to the paw-enterprise frontend while keeping both compatible with the existing `FilesPanel` component.

## Two API Generations

### Generation 1: Unified GET /files

The module-level `router` object maintains the Cluster E sub-PR 4 contract: a single `GET /files` endpoint returns a flat merged list of files from all sources. The `FilesPanel` frontend renders this list without caring which provider each row came from. This endpoint is served by the legacy `UnifiedFilesService` and is not tied to the `ProviderRegistry`.

This flat listing is intentionally preserved even as the v2 tree API ships. The frontend can migrate incrementally -- panels that render flat lists keep using `GET /files`; new navigation panels use the tree endpoints.

### Generation 2: Tree and Browse via build_router

`build_router` is a factory function that returns a new `APIRouter` wired to a specific `ProviderRegistry`, ABAC rule set, and request-context factory. This factory pattern is what allows `bootstrap.py` to inject concrete providers without the router module importing them directly.

The v2 endpoints include:
- `GET /files/tree` -- returns the full `FolderNode` tree of all mounts the user can see
- `GET /files/browse` -- returns paginated `FileEntry` items for a specific mount path
- `GET /files/{entry_id}` -- fetches a single entry by ID
- `POST /files/upload` -- uploads a file to a mount
- `PUT /files/{entry_id}/rename` -- renames an entry
- `DELETE /files/{entry_id}` -- deletes an entry

## Request Context Factory

`build_router` accepts a `request_context_factory` callable that extracts `RequestContext` from a FastAPI `Request`. This makes the router testable: tests can pass a factory that returns a fixed `RequestContext` without needing live authentication middleware.

## Error Handling

The router registers a `FilesError` exception handler that maps every `FilesError` subclass to its `http_status` and `code` attributes. This means provider code never raises `HTTPException` -- it raises typed domain errors and the router translates them.

## License Gating

The router imports `ee.cloud.license` and gates the files tree endpoints behind an EE license check. Requests without a valid EE license receive a 402 response before reaching any provider logic. The flat `GET /files` endpoint is not gated -- it existed before EE licensing was introduced.

## Known Gaps

- **No streaming download endpoint.** `open_stream` exists on providers but there is no `GET /files/{entry_id}/download` endpoint that streams the response. Downloads currently go through separate upload-specific routes.
- **No search endpoint in v2.** Provider `search` methods exist but no `GET /files/search` route is defined in this router.