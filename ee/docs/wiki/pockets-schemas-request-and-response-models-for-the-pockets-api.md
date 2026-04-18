---
{
  "title": "Pockets Schemas: Request and Response Models for the Pockets API",
  "summary": "Pydantic request and response schemas for the Pockets domain API. Defines validation rules for pocket creation (with inline agents, widgets, and ripple specs), updates, widget management, sharing controls, and collaborator management. Uses camelCase aliases to bridge the Python backend and JavaScript frontend conventions.",
  "concepts": [
    "Pydantic schema",
    "CreatePocketRequest",
    "UpdatePocketRequest",
    "AddWidgetRequest",
    "ShareLinkRequest",
    "AddCollaboratorRequest",
    "PocketResponse",
    "camelCase alias",
    "partial update",
    "request validation"
  ],
  "categories": [
    "Pockets",
    "API Layer",
    "Schemas",
    "Validation"
  ],
  "source_docs": [
    "b719f1eb6dc4b1d7"
  ],
  "backlinks": null,
  "word_count": 481,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Pockets Schemas: Request and Response Models for the Pockets API

## Purpose

These Pydantic models define the API contract between the frontend and the Pockets domain. They handle request validation (field types, lengths, patterns) and response serialization. The module docstring explicitly documents a design evolution: `CreatePocketRequest` was expanded to accept agents, widgets, and ripple specs inline so the frontend can create a fully-configured pocket in a single API call.

## Design Decisions

### Single-Call Pocket Creation
The `CreatePocketRequest` accepts `agents`, `ripple_spec`, and `widgets` fields so a pocket can be fully configured on creation. The module docstring explains: "the frontend can pass the full pocket spec on creation instead of requiring separate follow-up calls." This reduces the number of API round-trips from potentially 4+ (create pocket, then add agents, then add widgets, then set ripple spec) to 1.

### camelCase Alias Pattern
Fields like `session_id` (alias `sessionId`) and `ripple_spec` (alias `rippleSpec`) follow the codebase-wide convention of Python snake_case internally with camelCase aliases for JSON serialization. The `populate_by_name=True` model config allows both naming conventions.

### Validation Constraints
- `name`: `min_length=1, max_length=100` prevents empty names and excessively long ones
- `visibility`: regex pattern `^(private|workspace|public)$` enforces valid values
- `access` (ShareLinkRequest, AddCollaboratorRequest): regex pattern `^(view|comment|edit)$`
- These constraints fail fast at the API boundary, before business logic executes

### Optional Update Fields
`UpdatePocketRequest` uses `None` defaults for all fields, implementing partial updates. Only non-None fields are applied in the service layer. This is a standard PATCH semantics pattern.

### Response Model
`PocketResponse` defines the serialization contract but notably is NOT used as the actual return type in the router — the router returns raw `dict` from `_pocket_response()`. The `PocketResponse` model may exist for documentation/OpenAPI schema generation or for future migration to typed responses.

## Schema Summary

| Schema | Purpose |
|--------|---------|
| `CreatePocketRequest` | Create pocket with optional agents, widgets, ripple spec |
| `UpdatePocketRequest` | Partial update (all fields optional) |
| `AddWidgetRequest` | Add a widget to a pocket |
| `UpdateWidgetRequest` | Partial widget update |
| `ReorderWidgetsRequest` | Reorder widgets by ID list |
| `ShareLinkRequest` | Set share link access level |
| `AddCollaboratorRequest` | Add a collaborator with access level |
| `PocketResponse` | Serialization model (not currently used in router) |

## Known Gaps

- `PocketResponse` is defined but not used as the router's return type — the router returns `dict` instead. This means the response schema is not enforced and could drift from the actual response.
- `CreatePocketRequest.widgets` is typed as `list[dict]` (raw dicts) rather than a list of typed widget schemas. This means widget validation happens in the service layer, not at the schema level.
- `UpdateWidgetRequest` allows setting `data` to any type (`Any`), with no size constraint. A very large data payload could bloat the pocket document.
- No `DeletePocketRequest` or confirmation schema — deletion is idempotent and requires only the path parameter.
