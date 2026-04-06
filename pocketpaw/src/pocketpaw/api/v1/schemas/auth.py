# Auth schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Cookie-based login request."""

    token: str = Field(..., description="Master access token")


class SessionTokenResponse(BaseModel):
    """Session token exchange response."""

    session_token: str
    expires_in_hours: int


class TokenRegenerateResponse(BaseModel):
    """Token regeneration response."""

    token: str
