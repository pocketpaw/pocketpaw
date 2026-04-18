---
{
  "title": "Agents Domain Package Init (ee/cloud/agents/)",
  "summary": "Minimal package init that re-exports the agents router. Exists solely to make `ee.cloud.agents.router` importable as `ee.cloud.agents`.",
  "concepts": [
    "re-export",
    "package init",
    "agents domain"
  ],
  "categories": [
    "architecture",
    "enterprise",
    "cloud"
  ],
  "source_docs": [
    "782f8577c4e9d014"
  ],
  "backlinks": null,
  "word_count": 69,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Agents Domain Package Init

## Purpose

This is a minimal `__init__.py` that re-exports the `router` from `ee.cloud.agents.router`. The `# noqa: F401` suppresses the "imported but unused" linting warning since the import exists purely for re-export convenience.

## Why It Exists

Without this re-export, `mount_cloud()` would need to import `from ee.cloud.agents.router import router`. With it, `from ee.cloud.agents import router` also works, providing a cleaner API surface for the domain.