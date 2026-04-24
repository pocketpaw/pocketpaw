---
{
  "title": "Kits Router — PawKit Installation, Catalog, and Command Center Data Resolution",
  "summary": "The kits router manages PawKits — YAML-defined bundles of tools, skills, and UI panels that extend a PocketPaw agent's capabilities. It provides a catalog browser with installed-status flags, CRUD operations for installed kits, and a data-resolution endpoint that powers command center panels by fetching live data from configured sources.",
  "concepts": [
    "PawKits",
    "kit catalog",
    "YAML install",
    "InstallKitRequest",
    "command center",
    "data resolution",
    "installed status",
    "kit store",
    "kits scope",
    "FastAPI route ordering",
    "extension system"
  ],
  "categories": [
    "API",
    "Kits",
    "Extension System"
  ],
  "source_docs": [
    "d8090abf45bf07e3"
  ],
  "backlinks": null,
  "word_count": 443,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PawKits are the PocketPaw extension system. A kit bundles tools, agent skills, and optionally a command center UI panel into a single YAML-defined artifact that users can install with one click. The kits router is the REST interface that the dashboard's Kit Store and Command Center panels call.

## Catalog Before Instance Endpoints

The router comment explains a subtle FastAPI constraint:

```python
# Catalog endpoints (must be registered BEFORE /kits/{kit_id} to avoid
# FastAPI treating "catalog" as a kit_id path parameter)
```

`GET /kits/catalog` must be registered before `GET /kits/{kit_id}`. If the order were reversed, a request to `/kits/catalog` would match the path parameter route and `catalog` would be passed as a kit ID, likely resulting in a 404 or incorrect response. This is a FastAPI routing specificity issue that applies to any literal path segment that shares a prefix with a parameterized route.

## Catalog with Installed Status

`list_catalog()` merges two data sources: the static kit catalog (built-in and community kits) and the current store (installed kits). For each catalog entry, it computes an `installed` boolean by checking whether the kit's ID is in the installed set:

```python
installed_ids = {k.id for k in installed_kits}
# ... for entry in catalog: append with installed=entry.id in installed_ids
```

This lets the dashboard render a single unified catalog view where users can see what's available and what's already installed.

## YAML-Based Install

`InstallKitRequest` accepts raw YAML:

```python
class InstallKitRequest(BaseModel):
    yaml: str = Field(..., min_length=1, description="PawKit YAML configuration")
    kit_id: str | None = Field(default=None, description="Optional custom kit ID")
```

Users can install kits either from the catalog (by ID) or by pasting custom YAML. The optional `kit_id` override allows re-installing a catalog kit under a custom identifier, useful for running multiple instances of the same kit with different configurations.

## Data Resolution for Command Center Panels

`_resolve_source(source)` is an async helper that fetches live data for command center panel widgets. Each panel defines data sources in its YAML configuration, and this endpoint resolves them at render time — querying APIs, reading agent memory, or pulling from connectors — so panels display current data.

## Kits Scope Guard

The `kits` scope guard is applied at the router level, ensuring all kit operations require explicit authorization. This was added in the 2026-03-09 update, which also notes this as a retroactive addition — earlier versions of the kits router were accessible without scope restrictions.

## Known Gaps

The install endpoint accepts arbitrary YAML from the client. If the YAML parser or kit execution layer doesn't sufficiently sandbox the kit's capabilities, a maliciously crafted YAML payload could register tools with dangerous permissions or override existing kit configurations.