"""Request/response DTOs for the DeepWorkLog entity.

Wire shape uses camelCase to match the rest of the workspace surface.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DeepWorkLogOut(BaseModel):
    id: str
    workspaceId: str
    actorId: str
    action: str
    targetType: str
    targetId: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    at: str  # ISO-8601


class DeepWorkLogQuery(BaseModel):
    action: str | None = Field(default=None, max_length=120)
    actor: str | None = Field(default=None, max_length=120)
    since: str | None = None  # ISO-8601
    until: str | None = None  # ISO-8601
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class DeepWorkLogPageResponse(BaseModel):
    items: list[DeepWorkLogOut]
    nextCursor: str | None = None


__all__ = [
    "DeepWorkLogOut",
    "DeepWorkLogPageResponse",
    "DeepWorkLogQuery",
]
