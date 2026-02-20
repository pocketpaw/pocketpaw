# OAuth2 schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class TokenRequest(BaseModel):
    """Token exchange or refresh request."""

    grant_type: str = Field(..., pattern="^(authorization_code|refresh_token)$")
    code: str | None = None
    code_verifier: str | None = None
    client_id: str | None = None
    redirect_uri: str | None = None
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    """OAuth2 token response."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str


class RevokeRequest(BaseModel):
    """Token revocation request."""

    token: str
