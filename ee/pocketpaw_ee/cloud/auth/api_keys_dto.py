"""Wire DTOs for the API-key endpoints. camelCase keys match EE wire shape."""

from __future__ import annotations

from pydantic import BaseModel


class CreateAPIKeyRequest(BaseModel):
    name: str
    scopes: list[str]
    expires_in_days: int | None = None


class APIKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    ownerUserId: str  # noqa: N815
    expiresAt: str | None  # noqa: N815
    lastUsedAt: str | None  # noqa: N815
    createdAt: str  # noqa: N815
    revoked: bool


class CreatedAPIKeyResponse(APIKeyOut):
    fullKey: str  # noqa: N815


__all__ = ["APIKeyOut", "CreateAPIKeyRequest", "CreatedAPIKeyResponse"]
