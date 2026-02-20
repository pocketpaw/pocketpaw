# API key schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateKeyRequest(BaseModel):
    """Create a new API key."""

    name: str = Field(..., min_length=1, max_length=100)
    scopes: list[str] = Field(default=["chat", "sessions"])
    expires_at: str | None = None


class APIKeyInfo(BaseModel):
    """API key info (no secrets)."""

    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: str
    last_used_at: str | None = None
    expires_at: str | None = None
    revoked: bool = False


class APIKeyCreatedResponse(BaseModel):
    """Response when a new API key is created — plaintext shown once."""

    key: str  # Full plaintext — only shown at creation
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: str
    expires_at: str | None = None
