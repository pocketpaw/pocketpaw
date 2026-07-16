# domain.py — Frozen value objects for the Code Mode GitHub connection (CM-3).
# Created 2026-07-16 (feat/code-mode): tenancy fields (workspace_id, user_id) are
# REQUIRED at construction with no defaults per ee/cloud Rule 3 — a view is only
# ever built from a persisted, tenant-checked row, so there is no safe default for
# who owns a connection.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NewType

CodeConnectionId = NewType("CodeConnectionId", str)


@dataclass(frozen=True)
class CodeConnectionView:
    """Read model for one GitHub connection (an App installation binding)."""

    id: CodeConnectionId
    workspace_id: str
    user_id: str
    provider: str
    installation_id: str
    created_at: datetime
    updated_at: datetime
    # The GitHub account the App was installed on (display only). Optional.
    account_login: str | None = None


__all__ = ["CodeConnectionId", "CodeConnectionView"]
