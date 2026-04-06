# Plan mode schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class PlanActionRequest(BaseModel):
    """Approve or reject a plan."""

    session_key: str = Field(..., min_length=1)


class PlanActionResponse(BaseModel):
    """Result of plan approve/reject."""

    session_key: str
    action: str  # "approved" or "rejected"
