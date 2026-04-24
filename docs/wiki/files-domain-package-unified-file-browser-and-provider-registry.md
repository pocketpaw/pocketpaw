---
{
  "title": "Files Domain Package - Unified File Browser and Provider Registry",
  "summary": "The `ee/cloud/files/__init__.py` package entrypoint assembles and re-exports the complete public API for the files domain: the provider registry, ABAC schema types, pagination primitives, and the router factory function. It is the single import surface consumers use to mount the files subsystem.",
  "concepts": [
    "files domain",
    "FolderProvider",
    "ProviderRegistry",
    "ABAC",
    "MountConfig",
    "build_files_router",
    "FileEntry",
    "Capability",
    "provider pattern",
    "virtual file browser"
  ],
  "categories": [
    "files",
    "cloud EE",
    "ABAC",
    "architecture"
  ],
  "source_docs": [
    "3ee28b17c9633c85"
  ],
  "backlinks": null,
  "word_count": 297,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The files domain implements a virtualised file browser - a unified `/files` endpoint and Files Tab v2 with tree/browse navigation. Files can live in multiple backends (uploads storage, knowledge base), and the domain abstracts these behind a common `FolderProvider` protocol. The `__init__.py` assembles this ecosystem and exposes a curated `__all__` list.

## What Gets Exported

The exported names span four sub-modules:

- **`bootstrap.py`** - `build_files_router`, the factory that wires providers, mount configs, and ABAC rules into a ready-to-mount `APIRouter`.
- **`registry.py`** - `FolderProvider`, `ProviderRegistry`: the provider protocol and the registry that maps mount paths to provider instances.
- **`router.py`** - `build_router`, `router`: the base router builder and the default pre-built router.
- **`schemas.py`** - `Capability`, `FileEntry`, `FolderNode`, `MountConfig`, `Page`, `Permission`, `RequestContext`, `ResolvedMount`, `Scope`, `SearchQuery`: the full type vocabulary for the files domain.

## Architecture at a Glance

The files domain follows a provider/registry pattern:

1. **Providers** implement `FolderProvider` and know how to list, fetch, and search entries in a specific backend.
2. **Mounts** map URL path segments to providers (configured in `mounts.yaml`).
3. **ABAC rules** restrict which entries a given user can see based on tags and user attributes.
4. **The registry** resolves mount paths to providers at request time.
5. **The router** handles HTTP and delegates to the registry for each request.

## Why a Factory Rather Than a Module-Level Router?

`build_files_router` takes injected dependencies (`uploads_store`, `kb_service`, `ctx_factory`) rather than importing them at module load time. This makes the router testable in isolation - tests can inject mock stores - and avoids circular import issues that arise when the router imports from application-level modules.

## Known Gaps

- The `router` name exported here is the default pre-built router, which may not have all providers registered. Application startup code should prefer `build_files_router` for full configuration.