---
{
  "title": "Cloud Workspace Package Init",
  "summary": "Re-exports the workspace router so that importing ee.cloud.workspace gives immediate access to the FastAPI router. This is a standard Python package init with no business logic.",
  "concepts": [
    "package init",
    "re-export",
    "router"
  ],
  "categories": [
    "cloud",
    "workspace",
    "package structure"
  ],
  "source_docs": [
    "f1f0a9aa1f23a664"
  ],
  "backlinks": null,
  "word_count": 63,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Cloud Workspace Package Init

## Purpose

This `__init__.py` exists solely to re-export the `router` from `ee.cloud.workspace.router`. This lets the top-level application mount the workspace API by importing `from ee.cloud.workspace import router` without needing to know the internal module structure.

The `# noqa: F401` comment suppresses the "imported but unused" linting warning, which is expected for re-export-only init files.

## Known Gaps

None.
