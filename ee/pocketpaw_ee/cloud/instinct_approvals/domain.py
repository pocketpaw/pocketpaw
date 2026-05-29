# ee/pocketpaw_ee/cloud/instinct_approvals/domain.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — domain value
# object for the RFC 03 v2 template-level approval queue. Pure-Python
# frozen dataclass with REQUIRED tenancy fields (workspace_id has no
# default) — constructing an ``InstinctApproval`` domain object without
# tenancy is a type error. EE rule 3.

"""Domain value object for ``instinct_approvals``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class InstinctApproval:
    """One pending / decided template-level Instinct approval.

    Frozen so downstream readers (UI / audit / cross-service callers)
    cannot mutate the object after the service hands it back. Tenancy
    fields (``workspace_id``) are required positional so the type
    system catches a missing tenancy at construction time — the EE
    cloud rule 3 invariant.
    """

    id: str
    workspace_id: str
    pocket_id: str
    action_name: str
    row_id: str
    row_data: dict[str, Any]
    verdict: str
    reason: str
    matched_rules: list[dict[str, Any]]
    requested_at: datetime
    requested_by: str
    status: str
    decided_at: datetime | None = None
    decided_by: str | None = None
    park: dict[str, Any] | None = None
    created_at: datetime | None = None
    notify_rules: list[dict[str, Any]] = field(default_factory=list)


__all__ = ["InstinctApproval"]
