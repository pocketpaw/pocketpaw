"""SSO wire DTOs — request bodies + masked-secret responses."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SsoConfigUpsertRequest(BaseModel):
    provider: Literal["okta", "google", "azure", "generic_oidc"]
    issuer: str
    client_id: str
    client_secret: str
    allowed_domains: list[str] = Field(default_factory=list)
    enforced: bool = False


class SsoConfigOut(BaseModel):
    provider: str
    issuer: str
    client_id: str
    client_secret: str = "***"  # masked
    allowed_domains: list[str] = Field(default_factory=list)
    enforced: bool = False


class SsoTestResponse(BaseModel):
    ok: bool
    issuer: str | None = None
    endpoints: dict[str, str] | None = None
    error: str | None = None
