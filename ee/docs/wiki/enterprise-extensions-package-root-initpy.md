---
{
  "title": "Enterprise Extensions Package Root (__init__.py)",
  "summary": "The top-level __init__.py for PocketPaw's Enterprise Extensions (ee/) package. It serves as a module directory listing licensed enterprise features including Fabric, Instinct, Automations, and Audit subsystems.",
  "concepts": [
    "enterprise extensions",
    "FSL license",
    "ee package",
    "fabric",
    "instinct",
    "automations",
    "audit"
  ],
  "categories": [
    "architecture",
    "enterprise",
    "licensing"
  ],
  "source_docs": [
    "18a1a356641be653"
  ],
  "backlinks": null,
  "word_count": 239,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Enterprise Extensions Package Root

## Purpose

This file is the package initializer for `ee/` — PocketPaw's Enterprise Extensions. It contains no executable code; instead, it serves as documentation and a module map for the enterprise tier.

## License

The enterprise extensions are licensed under **FSL 1.1** (Functional Source License). All modules under `ee/` require a PocketPaw Enterprise license for production use. This is a deliberate separation from the open-source core — enterprise features live in their own namespace so the licensing boundary is clear at the import level.

## Module Map

| Module | Purpose |
|--------|---------|
| `api.py` | Singleton accessors (e.g., `get_instinct_store`) — bridges core tools to enterprise stores |
| `fabric/` | Ontology layer — objects, links, properties for structured enterprise data |
| `instinct/` | Decision pipeline — actions, approvals, audit trail for AI-assisted decisions |
| `automations/` | Time and data triggers — event-driven enterprise workflows |
| `audit/` | Enhanced compliance logging — extends instinct's audit with export formats and retention |

## Architecture Notes

The `ee/` package follows a domain-driven layout where each subdirectory is a self-contained domain. The `api.py` module at this level acts as a facade, providing singleton accessors so the core PocketPaw codebase can consume enterprise features without deep coupling to internal module paths.

## Known Gaps

- The file is purely documentary — no `__all__` exports or programmatic re-exports are defined, so IDE auto-import support is limited.