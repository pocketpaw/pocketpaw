---
{
  "title": "PawKit Catalog: Registry of Installable Dashboard Presets",
  "summary": "The PawKit Catalog is a curated in-memory registry of pre-configured PawKit types that users can browse and install from the Pocket Store. Each entry describes a dashboard preset with ID, display name, category, Lucide icon, and preview text — without bundling the full YAML configuration.",
  "concepts": [
    "KitCatalogEntry",
    "PawKit catalog",
    "Pocket Store",
    "get_all_catalog_kits",
    "get_builtin_yaml",
    "get_catalog_kit",
    "Lucide icons",
    "dashboard presets",
    "Mission Control",
    "Deep Work"
  ],
  "categories": [
    "kits",
    "catalog",
    "dashboard"
  ],
  "source_docs": [
    "c2b6d35a0bba42cf"
  ],
  "backlinks": null,
  "word_count": 347,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/kits/catalog.py` implements the browsable catalog of installable PawKits. It follows the same pattern as `src/pocketpaw/mcp/presets.py` — a Python list of dataclass entries acting as the registry, loaded eagerly at import time.

## KitCatalogEntry

Each catalog entry is a lightweight descriptor:

```python
@dataclass
class KitCatalogEntry:
    id: str          # e.g. "project-orchestrator"
    name: str        # e.g. "Project Orchestrator"
    description: str
    icon: str        # Lucide icon name
    category: str    # "general" | "research" | "engineering" | "content"
    author: str
    tags: list[str]
    preview: str     # short blurb for the store card
```

Entries intentionally do not embed the full `PawKitConfig` YAML. Keeping the catalog lean means the catalog can be loaded and rendered in the UI without parsing every kit's full configuration — full configs are only loaded on install.

## Built-In Catalog

The `_CATALOG` list ships with PocketPaw and includes kits such as:

| ID | Name | Category |
|----|------|----------|
| `mission-control` | Mission Control | general |
| `deep-work` | Deep Work | general |

Mission Control monitors multi-agent fleets; Deep Work manages goal decomposition and autonomous task execution.

## Catalog API

```python
# All catalog entries
entries = get_all_catalog_kits()

# Lookup by ID
entry = get_catalog_kit("mission-control")

# Load the YAML string for a specific kit (used during install)
yaml_str = get_builtin_yaml("mission-control")
```

`get_builtin_yaml()` reads the YAML file from `kits/builtins/{kit_id}.yaml` relative to the package, keeping the kit definition close to the code.

## Design Rationale: Static List vs. Database

The catalog is a hardcoded Python list rather than a database table. This mirrors how MCP presets work and avoids the operational overhead of migrations, seeds, and admin tooling. Adding a new built-in kit means adding one entry to `_CATALOG` and one YAML file — a two-file change reviewable in a PR.

## Known Gaps

- **No community catalog**: The catalog only contains PocketPaw-authored kits. There is no mechanism for users or third parties to submit catalog entries without a code change.
- **No versioning on entries**: `KitCatalogEntry` has no `version` field. If a built-in kit is updated, installed instances have no way to know they are outdated.