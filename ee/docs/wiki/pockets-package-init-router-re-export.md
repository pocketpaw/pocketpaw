---
{
  "title": "Pockets Package Init: Router Re-Export",
  "summary": "Package initializer that re-exports the pockets router for clean import paths. Allows other modules to import the router as `from ee.cloud.pockets import router` instead of the full submodule path.",
  "concepts": [
    "__init__.py",
    "re-export",
    "router",
    "package initialization"
  ],
  "categories": [
    "Pockets",
    "Package Structure"
  ],
  "source_docs": [
    "9ec67acc68710016"
  ],
  "backlinks": null,
  "word_count": 96,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Pockets Package Init: Router Re-Export

## Purpose

This `__init__.py` re-exports the FastAPI router from `ee.cloud.pockets.router` at the package level. This is a standard Python packaging pattern that provides cleaner import paths.

## What It Does

```python
from ee.cloud.pockets.router import router  # noqa: F401
```

The `# noqa: F401` suppresses the "imported but unused" linting warning — the import is intentionally for re-export, not direct use within this file.

This allows the application entrypoint to mount the router with:
```python
from ee.cloud.pockets import router
app.include_router(router)
```

Instead of the longer:
```python
from ee.cloud.pockets.router import router
```
