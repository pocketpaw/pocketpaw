# ee/pocketpaw_ee/cloud/storage/router.py — the storage read surface
# (feat/billing-storage-caps).
#
# One read route, scoped to the caller's CURRENT workspace (resolved via the
# standard ``current_workspace_id`` dep):
#   * GET /storage/usage — the workspace's storage usage vs its plan cap
#     (used_bytes / max_bytes / remaining_bytes / percent_used).
#
# THIN adapter per the "primitive = service + thin adapters" shape — all logic
# lives in ``storage.service`` (and the cap in ``billing.plans`` via
# entitlements). Mounted in ``mount_cloud()``.
#
# Created 2026-08-08 (feat/billing-storage-caps): new entity.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_workspace_id
from pocketpaw_ee.cloud.storage import service as storage_service
from pocketpaw_ee.cloud.storage.dto import StorageUsageResponse, storage_usage_to_dto

router = APIRouter(tags=["Storage"], dependencies=[Depends(require_license)])


@router.get("/storage/usage", response_model=StorageUsageResponse)
async def get_storage_usage(
    workspace_id: str = Depends(current_workspace_id),
) -> StorageUsageResponse:
    """Return the caller's workspace's storage usage vs its plan cap.

    ``used_bytes`` is the live S3 total (sum of the workspace's non-deleted
    uploaded-file blob sizes — the Files → Knowledge Base store). ``max_bytes``
    is the plan's storage cap (null = uncapped: Enterprise, or billing not
    enforced).
    """
    usage = await storage_service.resolve_storage_usage(workspace_id)
    return storage_usage_to_dto(usage)
