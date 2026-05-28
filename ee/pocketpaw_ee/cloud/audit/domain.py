# domain.py — Frozen value objects for the Audit entity.
# Created: 2026-05-17 — Tenancy fields required at construction per ee/cloud Rule 3.
from __future__ import annotations

from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Workspace-mutation audit event (Wave 2 Task 10).
# Tenancy fields (workspace_id, actor_id) are required at construction time
# per cloud Rule 3.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEventDomain:
    id: str
    workspace_id: str
    actor_id: str
    action: str
    target_type: str
    target_id: str | None
    metadata: dict[str, Any]
    ip: str | None
    user_agent: str | None
    at: datetime


@dataclass(frozen=True)
class AuditPage:
    items: list[AuditEventDomain] = field(default_factory=list)
    next_cursor: str | None = None


__all__ = [
    "AuditCategory",
    "AuditEntryId",
    "AuditEntryView",
    "AuditEventDomain",
    "AuditPage",
]
