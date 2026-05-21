# domain.py — Frozen value objects for the Audit entity.
# Created: 2026-05-17 — Tenancy fields required at construction per ee/cloud Rule 3.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NewType

AuditEntryId = NewType("AuditEntryId", str)
AuditCategory = Literal["decision", "data", "config", "security"]


@dataclass(frozen=True)
class AuditEntryView:
    id: AuditEntryId
    workspace_id: str
    timestamp: datetime
    actor: str
    action: str
    category: AuditCategory
    description: str
    pocket_id: str | None = None
    context: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    status: str = "completed"


__all__ = ["AuditEntryView", "AuditEntryId", "AuditCategory"]
