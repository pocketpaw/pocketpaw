---
{
  "title": "Fleet Install REST Router",
  "summary": "Exposes two HTTP endpoints — listing bundled fleet templates and triggering a fleet install — as part of the enterprise edition's fleet management subsystem. Guards the install route with RBAC and emits correlated audit events into the shared org journal.",
  "concepts": [
    "fleet templates",
    "fleet install",
    "RBAC permission guard",
    "org journal",
    "ActorSpec",
    "audit trail",
    "InstallFleetRequest",
    "FleetTemplatesResponse",
    "ee router pattern",
    "HTTP 403 guard",
    "FastAPI dependency injection"
  ],
  "categories": [
    "fleet management",
    "enterprise edition",
    "REST API",
    "audit and compliance"
  ],
  "source_docs": [
    "cf932278bb94b5f6"
  ],
  "backlinks": null,
  "word_count": 451,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The fleet router sits at `prefix="/fleet"` inside the EE router registry and is mounted at `/api/v1`, yielding two public endpoints: `GET /api/v1/fleet/templates` and `POST /api/v1/fleet/install`. Its job is to expose the Python-level fleet installer primitives over HTTP so paw-enterprise's `InstallFleetPanel` UI can list what is available and trigger an install without shelling out.

## Endpoints

### GET /fleet/templates

`get_templates()` calls an internal `_load_all_bundled()` helper that resolves every known bundled fleet name into a full `FleetTemplate` object and wraps the list in a `FleetTemplatesResponse` envelope. The envelope pattern is intentional — it keeps the response shape stable even if pagination or metadata fields are added later without breaking consumers that key on a top-level `templates` field.

### POST /fleet/install

The install endpoint accepts an `InstallFleetRequest` body carrying `workspace_id`, an optional `journal` flag (defaults to `True`), and an optional `ActorSpec` for caller identity forwarding. When `journal=True`, the handler injects the org-level `Journal` via `ee.journal_dep.get_journal` and passes it into `install_fleet`, so every install emits a correlated audit trio (start, success/failure) into the shared org event log. Setting `journal=False` skips journal emission entirely, which is useful in test environments or scripted bulk installs where the audit trail is unwanted noise.

## Security Model

A significant gap was fixed in `fix/fleet-install-auth-guard`: the original route only wired the `get_journal` dependency, meaning any authenticated user — or in practice any unauthenticated caller if the auth middleware was misconfigured — could trigger an install. The updated route calls `_require_fleet_install(user, workspace_id)` which raises `HTTPException(403)` unless the caller holds the `fleet:install` permission in the target workspace. This prevents privilege escalation where a viewer-role user could install arbitrary fleets into workspaces they do not own.

## Journal Consolidation

Before `feat/ee-journal-dep`, the fleet router opened its own SQLite journal at `~/.pocketpaw/journal/fleet.db`. This created a split audit trail — fleet events lived in one file while every other EE subsystem wrote to the org journal. The refactor replaced the local open with the shared `get_journal` FastAPI dependency, so all EE routes now append to the same org-level journal rooted at `SOUL_DATA_DIR` or `~/.soul/`.

## ActorSpec

The optional `ActorSpec` body field allows the HTTP caller to forward an actor identity — for example, the user ID of the human who clicked "Install" in the UI — into the journal event. Without it the journal records the service account that handles the request, which loses the human attribution chain needed for auditability.

## Known Gaps

- No rollback endpoint exists; a failed install leaves partial state that must be cleaned up manually.
- The `_load_all_bundled()` helper is synchronous and scans all bundled fleet names on every request — no caching. For large fleet catalogs this becomes a startup latency hit on the first request.