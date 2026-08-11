# ee/pocketpaw_ee/cloud/storage/domain.py — frozen value object for the storage
# metering entity (feat/billing-storage-caps).
#
# Pure-Python, framework-free shape. The service hands ``StorageUsage`` back
# across the entity boundary instead of leaking the Beanie ``FileUpload`` docs
# or raw dicts. ``used_bytes`` is the sum of the workspace's live (non-deleted)
# FileUpload blob sizes — the S3 bytes backing the Files → Knowledge Base store.
# ``max_bytes`` is the plan's ``max_storage_bytes`` cap (None = uncapped
# Enterprise, OR billing not enforced → the UI renders "Unlimited").
# ``remaining_bytes`` / ``percent_used`` derive from those two and are None when
# there's no cap to measure against.
#
# Created 2026-08-08 (feat/billing-storage-caps): new entity.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StorageUsage:
    """A workspace's storage usage vs its plan cap.

    ``used_bytes`` is always the live total (never None — a workspace with no
    files reads 0). ``max_bytes`` is the resolved cap: None means "uncapped" —
    either the Enterprise tier (negotiated, no plan ceiling) or billing not
    enforced (OSS / self-host, where the caps are informational). When
    ``max_bytes`` is None, ``remaining_bytes`` and ``percent_used`` are also
    None so callers render "Unlimited" instead of a misleading 0% or 0 left.
    """

    workspace_id: str
    used_bytes: int
    max_bytes: int | None
    remaining_bytes: int | None
    percent_used: float | None
