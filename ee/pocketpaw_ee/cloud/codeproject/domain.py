# domain.py — Frozen value objects for the Code Mode Project registry (CM-2a).
# Created 2026-07-16 (feat/code-mode): tenancy fields (workspace_id, user_id) are
# REQUIRED at construction with no defaults per ee/cloud Rule 3 — constructing a
# view without tenancy is a TypeError, so a leak can't be minted by omission.
# Mirrors websandbox/domain.py; a CodeProjectView is only ever built from a
# persisted, tenant-checked row.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NewType

CodeProjectId = NewType("CodeProjectId", str)


@dataclass(frozen=True)
class CodeProjectView:
    """Read model for one durable project row.

    Every field is required at construction — the tenancy fields
    (``workspace_id``, ``user_id``) most of all, per Rule 3. Optional runtime
    pointers (snapshot, current sandbox, last-opened) default to None so they
    land after the required tenancy fields without a second construction site.
    """

    id: CodeProjectId
    workspace_id: str
    user_id: str
    name: str
    provider: str
    repo: str
    created_at: datetime
    updated_at: datetime
    # Durable blob-storage snapshot pointer — the project's files between VMs.
    snapshot_file_id: str | None = None
    # The current ephemeral sandbox (a WebSandbox row id), null when none is live.
    current_sandbox_id: str | None = None
    last_opened_at: datetime | None = None


__all__ = [
    "CodeProjectId",
    "CodeProjectView",
]
