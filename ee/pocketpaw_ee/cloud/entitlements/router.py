# ee/pocketpaw_ee/cloud/entitlements/router.py — the entitlements read surface
# (BC-6, the Entitlement primitive).
#
# One read route, scoped to the caller's CURRENT workspace (resolved via the
# standard ``current_workspace_id`` dep):
#   * GET /entitlements — the workspace's resolved entitlements (plan + features
#     + monthly credit allotment).
#
# THIN adapter per the "primitive = service + thin adapters" shape — all logic
# lives in ``entitlements.service`` (and the catalog in ``billing.plans``).
# Mounted in ``mount_cloud()``. The plan CATALOG itself (GET /billing/plans) is a
# tenant-independent read and lives on the billing router.
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.entitlements import service as entitlements_service
from pocketpaw_ee.cloud.entitlements.dto import EntitlementsResponse, entitlements_to_dto
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_workspace_id

router = APIRouter(tags=["Entitlements"], dependencies=[Depends(require_license)])


@router.get("/entitlements", response_model=EntitlementsResponse)
async def get_entitlements(
    workspace_id: str = Depends(current_workspace_id),
) -> EntitlementsResponse:
    """Return the caller's workspace's resolved entitlements.

    Derived from the workspace's CURRENT ``Workspace.plan``: the resolved plan
    key, that tier's feature set, and its monthly credit allotment. A workspace
    with no/unknown plan resolves to the ``free`` base tier.
    """
    ent = await entitlements_service.resolve_entitlements(workspace_id)
    return entitlements_to_dto(ent)
