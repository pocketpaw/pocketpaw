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


# ---------------------------------------------------------------------------
# Workspace audit-event DTOs (Wave 2 Task 10).
#
# Wire shape uses camelCase to match the rest of the workspace surface
# (memberOut, inviteOut, etc.). Request / response classes are kept
# distinct per cloud Rule 4.
# ---------------------------------------------------------------------------


class AuditEventOut(BaseModel):
    id: str
    workspaceId: str
    actorId: str
    action: str
    targetType: str
    targetId: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    ip: str | None = None
    userAgent: str | None = None
    at: str  # ISO-8601


class AuditQueryRequest(BaseModel):
    action: str | None = Field(default=None, max_length=120)
    actor: str | None = Field(default=None, max_length=120)
    since: str | None = None  # ISO-8601 — parsed in service
    until: str | None = None  # ISO-8601
    cursor: str | None = None  # opaque, composite ``{at_iso}|{oid}``
    limit: int = Field(default=50, ge=1, le=100)


class AuditPageResponse(BaseModel):
    items: list[AuditEventOut]
    nextCursor: str | None = None


# ---------------------------------------------------------------------------
# SIEM webhook DTOs (Wave 3 Task 15).
# ---------------------------------------------------------------------------


class CreateAuditWebhookRequest(BaseModel):
    url: str = Field(..., max_length=2048)


class UpdateAuditWebhookRequest(BaseModel):
    enabled: bool | None = None


class AuditWebhookOut(BaseModel):
    id: str
    workspaceId: str
    url: str
    enabled: bool
    failureCount: int
    lastDeliveryAt: str | None = None
    lastStatus: int | None = None
    lastError: str | None = None
    createdBy: str
    createdAt: str
    secret: str | None = None  # populated only at creation / rotation


class RotatedSecretResponse(BaseModel):
    webhook: AuditWebhookOut
    secret: str


__all__ = [
    "AuditEntryDTO",
    "AuditEventOut",
    "AuditListResponse",
    "AuditPageResponse",
    "AuditQueryRequest",
    "AuditWebhookOut",
    "CreateAuditWebhookRequest",
    "ListAuditRequest",
    "RotatedSecretResponse",
    "UpdateAuditWebhookRequest",
]
