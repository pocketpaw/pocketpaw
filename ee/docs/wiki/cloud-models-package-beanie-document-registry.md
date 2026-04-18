---
{
  "title": "Cloud Models Package — Beanie Document Registry",
  "summary": "Re-exports all cloud document models and defines the `ALL_DOCUMENTS` list required for Beanie ODM initialization. This central registry ensures all document classes are discovered when the database connection starts.",
  "concepts": [
    "Beanie ODM",
    "document registry",
    "ALL_DOCUMENTS",
    "model re-exports",
    "MongoDB collections",
    "embedded models"
  ],
  "categories": [
    "models",
    "database",
    "package structure"
  ],
  "source_docs": [
    "e26b6fe72b760804"
  ],
  "backlinks": null,
  "word_count": 207,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Cloud Models Package — Beanie Document Registry

`cloud/models/__init__.py`

## Purpose

This `__init__.py` serves two roles:

1. **Convenience re-exports**: All model classes are importable directly from `ee.cloud.models` (e.g., `from ee.cloud.models import User, Agent`).
2. **Beanie initialization registry**: The `ALL_DOCUMENTS` list is passed to `beanie.init_beanie(document_models=ALL_DOCUMENTS)` during startup. Beanie uses this list to set up collection mappings, indexes, and event hooks. If a document class is missing from this list, it won't be usable.

## Registered Documents

- `User`, `Agent`, `Pocket`, `Session` — Core entities
- `Comment`, `Notification`, `FileObj` — Supporting entities
- `Workspace`, `Invite` — Multi-tenancy
- `Group`, `Message` — Chat domain

## Exported But Not in ALL_DOCUMENTS

Several types are exported for use as embedded models but are not standalone documents:
- `AgentConfig`, `CommentAuthor`, `CommentTarget` — Embedded sub-models
- `GroupAgent`, `Mention`, `Attachment`, `Reaction` — Embedded in Group/Message
- `OAuthAccount`, `WorkspaceMembership` — Embedded in User
- `NotificationSource`, `Widget`, `WidgetPosition` — Embedded in Notification/Pocket
- `WorkspaceSettings` — Embedded in Workspace

These are Pydantic `BaseModel` subclasses, not Beanie `Document` subclasses, so they don't need collection registration.

## Known Gaps

- If a new document model is added but not included in `ALL_DOCUMENTS`, it will silently fail at runtime when trying to query. There's no compile-time or startup-time check for completeness.
