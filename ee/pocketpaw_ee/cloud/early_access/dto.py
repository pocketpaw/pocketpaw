# ee/pocketpaw_ee/cloud/early_access/dto.py: wire shapes for the public
# early-access routes.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class WaitlistJoinRequest(BaseModel):
    """POST /early-access/waitlist. ``source`` names where the visitor came
    from (``ripple``, ``signup``), free text, for the launch dashboard."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    source: str | None = Field(default=None, max_length=64)


__all__ = ["WaitlistJoinRequest"]
