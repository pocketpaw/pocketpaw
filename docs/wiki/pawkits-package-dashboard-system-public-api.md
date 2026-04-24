---
{
  "title": "PawKits Package: Dashboard System Public API",
  "summary": "The `pocketpaw.kits` package exposes the complete public surface for PawKits — PocketPaw's configurable command-center dashboard system. It re-exports models, catalog entries, and store operations as a single flat namespace so consumers never need to import from internal submodules.",
  "concepts": [
    "PawKits",
    "KitCatalogEntry",
    "InstalledKit",
    "PawKitConfig",
    "FileKitStore",
    "PawKitMeta",
    "LayoutConfig",
    "PanelConfig",
    "namespace flattening",
    "public API boundary"
  ],
  "categories": [
    "kits",
    "dashboard",
    "package structure"
  ],
  "source_docs": [
    "03eff5e403c7dad0"
  ],
  "backlinks": null,
  "word_count": 254,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/kits/__init__.py` is the public API boundary for the PawKits subsystem. PawKits are installable dashboard layouts that turn PocketPaw into a configurable command center — combining metrics, kanban boards, feeds, and agent workflows in user-defined panels.

## Package Structure

The `__init__.py` aggregates exports from three submodules:

| Submodule | Responsibility |
|-----------|---------------|
| `kits.catalog` | Registry of pre-built kits users can install |
| `kits.models` | Pydantic schemas for kit configuration |
| `kits.store` | File-based persistence for installed kits |

## Why Flatten the Namespace?

By re-exporting everything through `__init__.py`, calling code uses:

```python
from pocketpaw.kits import PawKitConfig, InstalledKit, get_kit_store
```

Instead of reaching into internal submodules directly. If `FileKitStore` moves from `store.py` to a new `backends.py`, the public import path stays unchanged. This is the standard Python pattern for a package that evolves internally without breaking consumers.

## Full Public API

The `__all__` list defines the stable surface:

```python
__all__ = [
    "FileKitStore", "InstalledKit", "KitCatalogEntry",
    "LayoutConfig", "MetricItem", "PanelConfig",
    "PawKitConfig", "PawKitMeta", "SectionConfig",
    "UserConfigField", "WorkflowConfig",
    "get_all_catalog_kits", "get_builtin_yaml",
    "get_catalog_kit", "get_kit_store",
]
```

This covers the full kit lifecycle: discovering kits from the catalog, configuring them with `PawKitConfig`, installing them via `FileKitStore`, and rendering them with the panel/section/layout models.

## Relationship to Other Subsystems

- The **API router** (`src/pocketpaw/api/kits.py`) imports from this package to serve kit CRUD endpoints.
- The **frontend** (Alpine.js dashboard) consumes those endpoints, rendering panels defined by `PanelConfig` and `SectionConfig`.
- The **catalog** mirrors the pattern established in `src/pocketpaw/mcp/presets.py`, where built-in presets are registered in a Python list rather than a database.