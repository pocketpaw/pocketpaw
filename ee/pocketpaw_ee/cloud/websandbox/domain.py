# domain.py — Frozen value objects for the Web Cursor Sandbox Registry.
# Created 2026-07-15 (WC-1, feat/websandbox-registry): tenancy fields
# (workspace_id, user_id) are REQUIRED at construction with no defaults per
# ee/cloud Rule 3 — constructing a view without tenancy is a TypeError, so a
# leak can't be minted by omission.
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


__all__ = [
    "SandboxStatus",
    "WebSandboxId",
    "WebSandboxView",
]
