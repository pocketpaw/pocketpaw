"""Request/response DTOs for the RequestLog entity.

Wire shape uses camelCase to match the rest of the workspace surface.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RequestLogOut(BaseModel):
    id: str
    workspaceId: str
    actorId: str
    method: str
    path: str
    statusCode: int
    durationMs: float
    isError: bool
    ip: str | None = None
    userAgent: str | None = None
    at: str  # ISO-8601


class RequestLogQuery(BaseModel):
    method: str | None = Field(default=None, max_length=10)
    actor: str | None = Field(default=None, max_length=120)
    minStatus: int | None = Field(default=None, ge=100, le=599)
    maxStatus: int | None = Field(default=None, ge=100, le=599)
    isError: bool | None = None
    since: str | None = None  # ISO-8601
    until: str | None = None  # ISO-8601
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class RequestLogPageResponse(BaseModel):
    items: list[RequestLogOut]
    nextCursor: str | None = None


__all__ = [
    "RequestLogOut",
    "RequestLogPageResponse",
    "RequestLogQuery",
]
