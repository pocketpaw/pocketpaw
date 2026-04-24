---
{
  "title": "Pockets FastAPI Router: CRUD, Widgets, Sharing, and Layout Export",
  "summary": "This is the FastAPI router for the pockets domain, covering the full lifecycle of a pocket from creation through sharing, widget management, agent and team membership, and the newer layout export and user template endpoints. All routes are gated behind a license check dependency, and owner-only operations use explicit access guard dependencies to prevent privilege escalation.",
  "concepts": [
    "FastAPI router",
    "pockets",
    "layout export",
    "user templates",
    "share links",
    "collaborators",
    "widget management",
    "access control",
    "require_license",
    "require_pocket_owner",
    "CreateTemplateRequest",
    "ExportLayoutResponse",
    "session sub-resources"
  ],
  "categories": [
    "pockets",
    "routing",
    "EE cloud",
    "access control"
  ],
  "source_docs": [
    "bef4fdd4d8e95203"
  ],
  "backlinks": null,
  "word_count": 580,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/pockets/router.py` is the HTTP boundary for the pockets domain. It translates HTTP requests into calls on `PocketService` and `UserTemplateStore`, returning JSON-serialisable dicts or Pydantic response models. The router is entirely stateless — it holds no domain state of its own.

All routes on the router inherit `Depends(require_license)` via the `dependencies` argument on `APIRouter`. This means every endpoint in this file implicitly checks the EE license before executing, without each route handler needing to declare the dependency explicitly.

## Layout Export and User Templates (Cluster B)

Three routes added in Cluster B Sub-PR #3 address the absence of a layout save/share workflow:

- `POST /pockets/{pocket_id}/export-layout` — Calls `export_layout_yaml` from `layouts.py` and returns the YAML as a string. The route is read-only and safe to call on any pocket the caller can access; it produces no side effects and requires no ownership check beyond the standard fetch. The response model `ExportLayoutResponse` carries the `pocket_id` alongside the YAML so the frontend can correlate the response.

- `POST /pockets/templates` — Accepts a `CreateTemplateRequest` body that must include `yaml_source` (the YAML produced by export-layout or hand-authored). The route calls `parse_layout_yaml` to validate and extract the spec, then saves a `UserPocketTemplate` row to the injected `UserTemplateStore`. Malformed YAML is caught and re-raised as HTTP 400 with the human-readable error string — this prevents the UI from showing a generic 500 when a user pastes bad YAML.

- `GET /pockets/templates` — Lists all user-defined templates for the caller's active workspace. Workspace identity comes from the `current_workspace_id` dependency.

The `ExportLayoutRequest` model exposes optional override fields (`name`, `description`, `category`) that fall back to the pocket's own values when omitted. This lets operators export under a different display name without renaming the source pocket.

## CRUD and Access Control

The CRUD routes follow a consistent pattern: fetch the pocket via `PocketService`, check ownership or edit access, modify, and return. Ownership checks (`require_pocket_owner`) and edit-access checks (`require_pocket_edit`) are injected as route-level dependencies on the routes that need them, so the check happens before the handler body executes.

Visbility rules allow three levels — `private`, `workspace`, and `public` — validated by a regex pattern in `CreatePocketRequest`. Workspace-visible pockets can be read by any workspace member, but edit and share operations remain owner-gated.

## Widget Operations

Widgets are sub-documents inside a pocket. All widget operations delegate to `PocketService` which manipulates the `pocket.widgets` list and persists the whole pocket document. The reorder endpoint accepts an ordered list of widget IDs and rebuilds the list in that order, appending any widgets not mentioned at the tail — this prevents silent data loss if the frontend sends a partial list.

## Sharing and Collaboration

Share links use `secrets.token_urlsafe(32)` (in the service) to generate cryptographically random tokens. The router exposes generate, revoke, and update-access endpoints, all owner-gated. The token-based access endpoint `GET /shared/{token}` is intentionally unauthenticated — it is the read path for externally shared pockets.

Collaborators (`shared_with`) differ from team members: collaborators get edit access to the pocket content, while team members are workspace members associated with the pocket for organisational purposes.

## Known Gaps

- **In-process template store**: The `UserTemplateStore` injected into the template routes is not persisted. Restarting the server clears all saved templates.
- **No delete for templates**: There is no `DELETE /pockets/templates/{id}` endpoint in this iteration.
- **Session sub-resources**: `POST/GET /pockets/{id}/sessions` delegate to `SessionService` via a lazy import inside the handler, which is unusual compared to the top-of-file imports elsewhere. This avoids a circular import but makes the dependency implicit.