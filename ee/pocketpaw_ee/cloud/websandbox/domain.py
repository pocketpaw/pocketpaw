# domain.py — Frozen value objects for the Web Cursor Sandbox Registry.
# Created 2026-07-15 (WC-1, feat/websandbox-registry): tenancy fields
# (workspace_id, user_id) are REQUIRED at construction with no defaults per
# ee/cloud Rule 3 — constructing a view without tenancy is a TypeError, so a
# leak can't be minted by omission.
#
# Changed 2026-07-15 (WC-S3, feat/websandbox-s3-durability): added the optional
# ``snapshot_file_id`` — the pointer to the row's latest durable workspace
# snapshot in blob storage. Optional (default None) so it lands after the
# required tenancy fields without breaking the single construction site.
#
# Changed 2026-07-15 (WC-5a, feat/websandbox-edit-agent): added the optional
# ``branch`` — the auto-created ``paw/edit-<hex>`` feature branch the repo is
# checked out onto in the VM so AI edits never touch the default branch. Optional
# (default None) so it lands after the required tenancy fields.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NewType

WebSandboxId = NewType("WebSandboxId", str)

# The lifecycle a sandbox row moves through. Kept as a Literal so an unknown
# state is a type error at the boundary rather than a silent string.
SandboxStatus = Literal["pending", "opening", "ready", "stopped", "reaped"]


@dataclass(frozen=True)
class WebSandboxView:
    """Read model for one sandbox row.

    Every field is required at construction — the tenancy fields
    (``workspace_id``, ``user_id``) most of all, per Rule 3. A view is only
    ever built from a persisted, tenant-checked row, so there is no safe
    default for who owns it.
    """

    id: WebSandboxId
    workspace_id: str
    user_id: str
    repo: str
    status: str
    sandbox_id: str | None
    installation_id: str | None
    created_at: datetime
    updated_at: datetime
    # Pointer to the latest durable workspace snapshot in blob storage (WC-S3).
    snapshot_file_id: str | None = None
    # The auto-created ``paw/edit-<hex>`` feature branch checked out in the VM
    # so AI edits never touch the default branch (WC-5a).
    branch: str | None = None


__all__ = [
    "SandboxStatus",
    "WebSandboxId",
    "WebSandboxView",
]
