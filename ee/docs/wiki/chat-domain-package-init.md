---
{
  "title": "Chat Domain Package Init",
  "summary": "Package initializer for the chat domain that re-exports the router. This allows the main application to import the chat router directly from the package.",
  "concepts": [
    "chat domain",
    "package init",
    "router re-export"
  ],
  "categories": [
    "chat",
    "package structure"
  ],
  "source_docs": [
    "6d9ca39e4ec93969"
  ],
  "backlinks": null,
  "word_count": 93,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Chat Domain Package Init

`cloud/chat/__init__.py`

## Purpose

This `__init__.py` exposes the chat domain's FastAPI router at the package level. By re-exporting `router` from `ee.cloud.chat.router`, the main app can simply do `from ee.cloud.chat import router` without knowing the internal module structure.

The `# noqa: F401` suppresses the "imported but unused" linting warning, since the import exists purely for re-export.

## Module Structure

The chat domain contains:
- `router.py` — REST endpoints + WebSocket handler
- `schemas.py` — Pydantic request/response models
- `service.py` — Business logic (GroupService, MessageService)
- `ws.py` — WebSocket connection manager
