---
{
  "title": "Files Bootstrap - Composing Providers, Mounts, and Rules into a Router",
  "summary": "`bootstrap.py` is the composition root for the files domain: it reads mount configuration, registers the built-in providers (uploads and knowledge base), loads ABAC rules, and returns a fully-configured `APIRouter` ready to be mounted by the application. It is the only place where concrete dependencies are wired together.",
  "concepts": [
    "composition root",
    "dependency injection",
    "ProviderRegistry",
    "UploadsProvider",
    "KbProvider",
    "build_files_router",
    "MountConfig",
    "ABAC rules",
    "factory function",
    "ctx_factory"
  ],
  "categories": [
    "files",
    "cloud EE",
    "architecture",
    "dependency injection"
  ],
  "source_docs": [
    "58ca72237638ef45"
  ],
  "backlinks": null,
  "word_count": 340,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`build_files_router` is a factory function that takes three injected dependencies and returns a complete FastAPI `APIRouter`:

```python
def build_files_router(
    *,
    uploads_store,
    kb_service,
    ctx_factory: Callable[[Request], RequestContext],
) -> APIRouter:
    registry = ProviderRegistry(configs=load_mounts())
    registry.register(UploadsProvider(store=uploads_store))
    registry.register(KbProvider(service=kb_service))
    rules = load_rules()
    return build_router(registry=registry, rules=rules, ctx_factory=ctx_factory)
```

This is the only location in the files domain where concrete implementations (`UploadsProvider`, `KbProvider`) are bound to the abstract `FolderProvider` protocol. Everything else works through interfaces.

## Composition Steps

1. **Load mounts** - `load_mounts()` reads `mounts.yaml` and produces a list of `MountConfig` objects that map URL path prefixes to provider IDs.
2. **Build registry** - `ProviderRegistry(configs=...)` holds the mount configurations and an initially empty provider map.
3. **Register providers** - `UploadsProvider` wraps the file upload storage backend; `KbProvider` wraps the knowledge base service. Both implement `FolderProvider`.
4. **Load ABAC rules** - `load_rules()` reads `abac_rules.yaml` and produces an `AbacRuleSet`.
5. **Build router** - `build_router(registry, rules, ctx_factory)` constructs the FastAPI router that handles HTTP requests by delegating to the registry.

## Why a Factory?

Application startup code calls `build_files_router(uploads_store=..., kb_service=..., ctx_factory=...)`. This deferred construction avoids module-level side effects (file reads, service instantiation) at import time. Tests can pass in mock stores and services without monkey-patching module globals.

## `ctx_factory`

The `ctx_factory` callable converts a raw `fastapi.Request` into a `RequestContext` - a typed object carrying the authenticated user, their attributes, workspace ID, and any other per-request data the files domain needs. Injecting this as a factory keeps the files domain decoupled from the application's auth and session management code.

## Provider Registration Order

Providers are registered in code order: uploads first, then kb. The registry uses `provider_id` (a string each provider declares) to look up providers at request time, so registration order has no effect on routing.

## Known Gaps

- `build_files_router` registers exactly two providers. Adding a third (e.g., S3, SharePoint) requires modifying this function; a more extensible design would accept a list of providers as a parameter.
- Error handling around `load_mounts()` and `load_rules()` is minimal - a malformed YAML file will cause an unhandled exception at startup.