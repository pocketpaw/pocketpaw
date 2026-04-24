---
{
  "title": "Pockets Domain Schemas: Request Validation and Response Model Tests",
  "summary": "This test file validates all Pydantic request/response schemas in the pockets domain — covering field defaults, enum constraints, length limits, optional-field behavior, and the full `PocketResponse` shape. It ensures the API surface contracts are locked in and that invalid inputs are rejected at the schema layer before reaching business logic.",
  "concepts": [
    "CreatePocketRequest",
    "AddWidgetRequest",
    "UpdatePocketRequest",
    "ShareLinkRequest",
    "ReorderWidgetsRequest",
    "AddCollaboratorRequest",
    "PocketResponse",
    "Pydantic validation",
    "visibility enum",
    "default values",
    "PATCH semantics"
  ],
  "categories": [
    "testing",
    "schema validation",
    "pockets",
    "API design",
    "test"
  ],
  "source_docs": [
    "212703970d9dd2da"
  ],
  "backlinks": null,
  "word_count": 497,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The pockets domain exposes a rich CRUD API for creating and managing AI agent workspaces. Every HTTP request is validated by a Pydantic schema before reaching the route handler. This test file pins the behavior of those schemas — specifically their defaults, constraints, and validation errors — without needing a running server.

## CreatePocketRequest

**Defaults** — `visibility` defaults to `"workspace"` (private-to-workspace, not public, not private-to-owner). `session_id` defaults to `None`. These are the expected defaults for a new pocket created from the UI: visible to all workspace members but not publicly accessible.

**Name constraints** — Empty names are rejected (`test_create_pocket_empty_name_rejected`). Names longer than 100 characters are rejected (`test_create_pocket_name_too_long`). These constraints prevent blank or unusably long pocket names from reaching the database.

**Visibility enum** — Only `"private"`, `"workspace"`, and `"public"` are valid. `test_visibility_validation` confirms an invalid value like `"invalid"` raises `PydanticValidationError`. This prevents silent acceptance of unrecognized values that would be stored and then confuse authorization checks.

**All fields** — `test_create_pocket_all_fields` confirms that all optional fields (`description`, `type`, `icon`, `color`, `session_id`) are accepted when provided.

## ShareLinkRequest

`access` defaults to `"view"`. Only `"view"` and `"edit"` are valid — `"admin"` is rejected. This enum constraint ensures that a share link can only grant view or edit access, never full ownership.

## AddWidgetRequest

**Defaults** — `type` defaults to `"custom"`, `data_source_type` defaults to `"static"`. These defaults make the minimal widget creation request (`{"name": "Chart"}`) produce a sensible widget with explicit values for all required fields.

**Name constraints** — Same as pocket names: no empty strings, max 100 characters.

**All fields** — `test_add_widget_all_fields` confirms that `span`, `data_source_type`, `config`, `props`, and `assigned_agent` are all accepted.

## UpdateWidgetRequest and UpdatePocketRequest

Both schemas make all fields optional (`test_update_widget_all_optional`, `test_update_pocket_all_optional`). This supports PATCH semantics: a client can update only the fields it wants to change without re-sending unchanged fields. `test_update_widget_partial` confirms that providing only `name` and `config` works correctly.

`test_update_pocket_with_ripple_spec` confirms that `ripple_spec` (the visual layout definition for the pocket's dashboard) can be passed as a nested dict and is accessible on the schema instance.

## ReorderWidgetsRequest

Accepts an ordered list of widget IDs. Empty lists are valid (`test_reorder_widgets_empty`). This allows clients to remove all widgets from a pocket's order by sending an empty list.

## AddCollaboratorRequest

`access` defaults to `"edit"`. Only `"view"`, `"comment"`, and `"edit"` are valid — `"admin"` is rejected. The default of `"edit"` reflects the expected workflow: when a user explicitly adds a collaborator, they usually intend for that person to be able to edit.

## PocketResponse

`test_pocket_response_model` constructs a `PocketResponse` with all required fields and verifies the schema accepts it. This confirms the response model can serialize a pocket document without field missing errors.

## Known Gaps

- **No test for `ripple_spec` schema structure** — `UpdatePocketRequest.ripple_spec` accepts any dict. The ripple spec format (which widgets, what layout) is not validated at the schema level.
- **No test for `color` validation** — The `color` field accepts any string. Invalid hex codes or CSS color names would pass validation and reach the database.
