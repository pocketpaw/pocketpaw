# dto.py — Request/response DTOs for the Audit entity.
# Created: 2026-05-17 — Mirrors the legacy /runtime/audit envelope so the
#   frontend mapper (mapAuditEntry) keeps working unchanged.
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ListAuditRequest(BaseModel):
    q: str | None = Field(default=None, max_length=200)
    category: Literal["decision", "data", "config", "security"] | None = None
    pocket_id: str | None = None
    actor: str | None = None
    limit: int = Field(default=200, ge=1, le=1000)
    cursor: str | None = None  # forward-compat; unused in B1


class AuditEntryDTO(BaseModel):
    id: str
    timestamp: str  # ISO-8601 UTC
    pocket_id: str | None = None
    actor: str
    action: str
    category: str
    description: str
    context: dict[str, Any] = Field(default_factory=dict)
    ai_recommendation: str | None = None
    outcome: str | None = None
    status: str = "completed"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditListResponse(BaseModel):
    entries: list[AuditEntryDTO]
    total: int


__all__ = ["ListAuditRequest", "AuditListResponse", "AuditEntryDTO"]
