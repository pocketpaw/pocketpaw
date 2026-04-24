---
{
  "title": "Pockets Domain Pydantic Schemas: Request and Response Models",
  "summary": "This file defines all Pydantic request and response models for the pockets domain, covering creation, update, widget management, reordering, share links, collaborators, and the full pocket response shape. A notable recent addition is support for passing agents, rippleSpec, and an initial widgets list on creation so the frontend can fully specify a pocket in a single request.",
  "concepts": [
    "Pydantic",
    "BaseModel",
    "CreatePocketRequest",
    "UpdatePocketRequest",
    "rippleSpec",
    "widget schema",
    "visibility validation",
    "share link",
    "collaborator",
    "AddWidgetRequest",
    "ReorderWidgetsRequest",
    "camelCase alias",
    "populate_by_name"
  ],
  "categories": [
    "pockets",
    "schemas",
    "Pydantic",
    "EE cloud"
  ],
  "source_docs": [
    "4571672f133d7f6b"
  ],
  "backlinks": null,
  "word_count": 531,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/pockets/schemas.py` is the data contract layer between HTTP and the pockets service. All Pydantic models here enforce type safety at the API boundary before any business logic executes, converting raw JSON into typed Python objects and providing clear validation error messages when fields are wrong.

## CreatePocketRequest

The creation schema accepts the full pocket spec upfront:

```python
class CreatePocketRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    visibility: str = Field(default="workspace", pattern="^(private|workspace|public)$")
    agents: list[str] = Field(default_factory=list)
    ripple_spec: dict | None = Field(default=None, alias="rippleSpec")
    widgets: list[dict] = Field(default_factory=list)
    model_config = {"populate_by_name": True}
```

The `agents`, `ripple_spec`, and `widgets` fields were added to remove the need for multiple sequential API calls when creating an agent-populated or layout-driven pocket. Before this change, the frontend had to POST to create the pocket, then POST again to add each agent or widget. The single-request approach is especially important for agent-generated pockets that carry a full ripple spec.

The `alias="rippleSpec"` with `populate_by_name=True` means the field can be sent from the frontend as either camelCase (`rippleSpec`) or snake_case (`ripple_spec`). This is a deliberate flexibility choice to support both the frontend JavaScript convention and the Python backend convention without requiring a transformation layer.

The `visibility` regex pattern validates that only the three allowed values can be set, failing fast at the schema layer before any database write attempt.

## UpdatePocketRequest

All fields are optional to support partial updates (PATCH semantics). The `ripple_spec` field uses the same alias pattern as `CreatePocketRequest`. Notably, `visibility` changes are allowed through this schema but the service enforces an additional owner check — only the pocket owner can change visibility even if they have edit access. This two-layer approach means the schema stays permissive (any editor could send a `visibility` field) while the service enforces the real rule.

## Widget Schemas

`AddWidgetRequest` carries the full widget spec including `span` (grid column span), `data_source_type`, and `assigned_agent`. The `span` field with a default of `"col-span-1"` reflects the Tailwind CSS grid class system used in the frontend.

`UpdateWidgetRequest` uses `Any` for the `data` field because widget data can be any JSON structure depending on widget type — it could be a string, list, dict, or number.

`ReorderWidgetsRequest` is intentionally minimal: it only carries an ordered list of widget IDs. The service handles the mapping from IDs back to Widget objects and preserves widgets not included in the list.

## ShareLinkRequest and AddCollaboratorRequest

Both access level fields use regex validation:

- `ShareLinkRequest.access`: `^(view|comment|edit)$`
- `AddCollaboratorRequest.access`: `^(view|comment|edit)$`

This prevents invalid access levels from reaching the database.

## PocketResponse

The response model explicitly lists every field the frontend expects. It includes both camelCase-aliased fields (like `share_link_token`) and raw Python names, relying on the service's `_pocket_response` serialiser function to map MongoDB document fields to the right names. The `ripple_spec` field is optional (`None`) because legacy pockets may not have one.

## Known Gaps

- **Untyped widget dicts**: `widgets: list[dict]` in `CreatePocketRequest` and `PocketResponse` accepts any dict structure. There is no Widget schema enforced at the API layer; validation only happens at the model layer when constructing `Widget` objects.
- **No read schema for templates**: The `UserTemplateResponse` model lives in `router.py` rather than here, breaking the convention of keeping all domain schemas in `schemas.py`.