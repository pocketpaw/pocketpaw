# ee/pocketpaw_ee/cloud/storage/dto.py — response schema for the storage read
# surface (feat/billing-storage-caps).
#
# Read-only surface, so there is no request DTO. ``StorageUsageResponse`` mirrors
# ``storage.domain.StorageUsage`` for ``GET /storage/usage``. ``max_bytes`` /
# ``remaining_bytes`` / ``percent_used`` are null when the workspace is uncapped
# (Enterprise) or billing is not enforced — the Settings page renders
# "Unlimited" in that case.
#
# Created 2026-08-08 (feat/billing-storage-caps): new entity.

from __future__ import annotations

from pydantic import BaseModel

from pocketpaw_ee.cloud.storage.domain import StorageUsage


class StorageUsageResponse(BaseModel):
    """A workspace's storage usage vs its plan cap — response of GET /storage/usage.

    ``used_bytes`` is the live S3 total (sum of the workspace's non-deleted
    ``FileUpload`` blob sizes); ``max_bytes`` is the plan's ``max_storage_bytes``
    (null = uncapped — Enterprise, or billing not enforced). ``remaining_bytes``
    and ``percent_used`` derive from those two and are null when ``max_bytes`` is
    null.
    """

    workspace_id: str
    used_bytes: int
    max_bytes: int | None = None
    remaining_bytes: int | None = None
    percent_used: float | None = None


def storage_usage_to_dto(usage: StorageUsage) -> StorageUsageResponse:
    """Map a frozen ``domain.StorageUsage`` to its wire DTO."""
    return StorageUsageResponse(
        workspace_id=usage.workspace_id,
        used_bytes=usage.used_bytes,
        max_bytes=usage.max_bytes,
        remaining_bytes=usage.remaining_bytes,
        percent_used=usage.percent_used,
    )
