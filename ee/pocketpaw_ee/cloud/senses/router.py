# Senses — FastAPI router (cloud 4-file rule).
# Created: 2026-07-16 (SR-2 catalog listing API) — exposes
#   ``GET /api/v1/cloud/senses/catalog`` (mounted at /api/v1/cloud/senses via
#   mount_cloud()). The browsable connector catalog: every connector grouped by
#   category, each with its actions + trust + senses + availability, and a
#   per-tenant ``bound`` flag. Models the connectors router's active-workspace dep
#   chain — NO ``{workspace_id}`` path param (the workspace resolves from
#   ``current_workspace_id``, which the UI auto-threads via X-Workspace-Id), and
#   license-gated at the router level. Read-only browse, so no RBAC action guard
#   beyond the license + workspace tenant gate (mirrors the connectors GET list
#   route, which carries only ``require_license`` + ``current_workspace_id``).
#   ``pocket_id`` is an optional query param; absent -> workspace-scope, so the
#   bound overlay counts only workspace-scoped connectors.
# Updated: 2026-07-16 (SR-9 security fix) — the optional ``pocket_id`` overlay is
#   now gated behind a pocket-access check. Without it a workspace member could
#   pass another member's private pocket ObjectId and read that pocket's
#   pocket-scoped connector bindings (a within-tenant IDOR-style metadata leak,
#   caught at the SR-9 security-review gate). The route now resolves the caller
#   (``current_user_id``) and honors ``pocket_id`` ONLY when the caller has
#   run-access to that pocket (``pockets.service.has_action_run_access`` — the same
#   member-level boundary the pocket-scoped connector surfaces enforce); otherwise
#   it SILENTLY falls back to workspace-scope (``pocket_id=None``). Fallback, not a
#   403 — a 403-vs-200 difference would be a pocket-existence oracle.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.senses import service as senses_service
from pocketpaw_ee.cloud.senses.dto import CatalogCategoryResponse
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

# Mounted under /api/v1/cloud/senses. The catalog is the discovery front door
# for connectors: the connectors router owns workspace enable/disable state,
# this router owns the browsable capability catalog (every connector the
# registry knows) with per-tenant bound state overlaid.
router = APIRouter(
    prefix="/cloud/senses",
    tags=["Senses"],
    dependencies=[Depends(require_license)],
)


@router.get("/catalog", response_model=list[CatalogCategoryResponse])
async def list_catalog(
    pocket_id: str | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[CatalogCategoryResponse]:
    """Browse the whole connector catalog, grouped by category.

    Returns every connector the registry knows (not just the ones this workspace
    bound), each with its actions (trust level + execution mode + availability +
    a ``cost_estimate`` placeholder), its declared senses, and a ``bound`` flag
    resolved for this workspace / pocket. ``local`` / ``sandbox`` actions the
    shared cloud can't dispatch are marked unavailable.

    Tenant-filtered on workspace (via the EE connectors store read) — no other
    workspace's bound state can leak. ``pocket_id`` is optional; absent, the bound
    overlay counts only workspace-scoped connectors.

    The pocket-scoped overlay is honored ONLY when the caller has run-access to
    that pocket; otherwise ``pocket_id`` is dropped and the overlay falls back to
    workspace-scope. This stops a member from reading another member's private
    pocket bindings by passing that pocket's id (SR-9). The fallback is silent (no
    403) so the response can't be used as a pocket-existence oracle.
    """
    if pocket_id:
        try:
            has_access = await pockets_service.has_action_run_access(pocket_id, user_id)
        except Exception:  # noqa: BLE001 — any resolve failure → treat as no access, fall back
            has_access = False
        if not has_access:
            pocket_id = None
    return await senses_service.list_catalog(workspace_id, pocket_id=pocket_id)
