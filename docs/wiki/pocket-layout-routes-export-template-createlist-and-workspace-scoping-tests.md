---
{
  "title": "Pocket Layout Routes: Export, Template Create/List, and Workspace Scoping Tests",
  "summary": "This integration test suite covers three new routes on the pockets router that implement the layout save/share surface: `POST /pockets/{id}/export-layout` (YAML serialization), `POST /pockets/templates` (template creation), and `GET /pockets/templates` (workspace-scoped listing). It verifies the round-trip fidelity, metadata override support, cross-workspace isolation, and malformed YAML handling.",
  "concepts": [
    "pocket layout",
    "export-layout",
    "YAML serialization",
    "PocketLayout",
    "template store",
    "workspace scoping",
    "rippleSpec",
    "round-trip",
    "parse_layout_yaml",
    "cross-workspace isolation",
    "template CRUD"
  ],
  "categories": [
    "testing",
    "pocket layouts",
    "API",
    "workspace isolation",
    "test"
  ],
  "source_docs": [
    "beeff7f1c107d846"
  ],
  "backlinks": null,
  "word_count": 473,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's pocket layout feature allows users to export a pocket's visual configuration as a YAML document, save it as a reusable template, and later instantiate new pockets from that template. This test file covers the three HTTP endpoints that expose this workflow, with `PocketService.get` monkeypatched to return a canned fixture instead of querying MongoDB.

## Fixture Design

The `app` fixture builds a FastAPI app with the pockets router and overrides multiple dependencies:

- **`PocketService.get`** — Returns a fixed `POCKET_FIXTURE` dict (containing a `rippleSpec` with two widgets and a `"2-col"` layout) regardless of the pocket ID requested.
- **Auth guards** — `require_license`, `require_pocket_edit`, `require_pocket_owner` are replaced with no-ops.
- **Identity deps** — `current_user_id` and `current_workspace_id` return `FAKE_USER` and `FAKE_WORKSPACE` constants.

An `autouse` fixture calls `reset_user_template_store()` before and after every test, ensuring the in-process template store is clean. This matters because the store is a module-level singleton — without the reset, a template created in one test would persist into the next.

## Export Layout (`TestExportLayout`)

**`test_returns_yaml_carrying_the_rippleSpec`** — `POST /pockets/pocket-1/export-layout` with an empty body returns a JSON object with `pocket_id` and `yaml` fields. The YAML contains `kind: PocketLayout` and the serialized `rippleSpec`. `parse_layout_yaml(yaml_text)` recovers the original spec dict, confirming round-trip fidelity.

**`test_metadata_overrides_take_effect`** — Passing `{"name": "Shared Dashboard", "description": "Shipped", "category": "custom"}` in the request body causes those values to appear in the YAML. This supports the UI flow where a user names a template before exporting.

## Template Create and List (`TestCreateAndListTemplate`)

**`test_create_then_list_shows_the_template`** — The test chains export → create → list:

1. Export the pocket's layout to YAML.
2. `POST /pockets/templates` with the YAML document creates a template scoped to `FAKE_WORKSPACE`.
3. `GET /pockets/templates` returns the template in the list.

This end-to-end chain confirms the three routes work together as a coherent workflow.

## Workspace Scoping (`TestWorkspaceScoping`)

**`test_other_workspace_templates_do_not_surface`** — Seeds a template directly into the store under workspace `"ws-beta"`, then queries via the test client (which is authenticated as `FAKE_WORKSPACE = "ws-alpha"`). The GET response must not include `ws-beta`'s template. This verifies the listing is scoped to the authenticated workspace, preventing cross-tenant template leakage.

## Malformed YAML Handling (`TestMalformedYaml`)

**`test_missing_spec_returns_400`** — POSTing valid YAML that lacks a `spec:` key returns HTTP 400 with a human-readable `detail`, not HTTP 500. This confirms `parse_layout_yaml` raises `ValueError` on invalid input and the router converts that to a 400.

## Round-Trip (`TestRoundTrip`)

**`test_export_then_create_then_list_reproduces_the_layout`** — The full captain's demo flow: export a pocket, save it as a template, list templates, and confirm the stored spec equals the original `rippleSpec`. This is the highest-confidence test in the file.

## Known Gaps

- The tests bypass all RBAC guards. There is no test for the 403 path when a user doesn't have `pocket_edit` permission.
- The `reset_user_template_store` pattern is specific to the in-process store. When the store is backed by a database, this reset mechanism will need to change.
