# Common API response schemas.
# Created: 2026-02-20

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel):
    """Base response wrapper."""

    model_config = {"from_attributes": True}


class ErrorResponse(APIResponse):
    """Standard error envelope."""

    detail: str
    code: str | None = None


class PaginatedResponse(APIResponse, Generic[T]):
    """Paginated list response."""

    items: list[T]
    total: int
    offset: int = 0
    limit: int = 50


class OkResponse(APIResponse):
    """Simple success response."""

    ok: bool = True


class StatusResponse(APIResponse):
    """Status string response."""

    status: str = "ok"
