# ee/pocketpaw_ee/cloud/launch_controls/dto.py: wire shapes for the operator
# read/write of the launch controls (``/api/v1/platform/launch-controls``).

from __future__ import annotations

from pydantic import BaseModel, Field


class LaunchControlsUpdateRequest(BaseModel):
    """A partial update: only the fields that are set change. ``reason`` is
    recorded on the platform audit event and is required."""

    signups_paused: bool | None = None
    free_usage_paused: bool | None = None
    daily_free_ceiling_credits: int | None = Field(default=None, ge=0)
    reason: str = ""


class LaunchControlsResponse(BaseModel):
    signups_paused: bool
    free_usage_paused: bool
    daily_free_ceiling_credits: int


__all__ = ["LaunchControlsResponse", "LaunchControlsUpdateRequest"]
