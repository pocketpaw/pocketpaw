"""Workspace document — one per deployment/org."""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WorkspaceSettings(BaseModel):
    default_agent: str | None = None  # Agent ID
    allow_invites: bool = True
    retention_days: int | None = None  # None = keep forever


class SsoConfig(BaseModel):
    """Embedded OIDC SSO config — one per workspace, optional."""

    provider: str  # okta | google | azure | generic_oidc
    issuer: str
    client_id: str
    client_secret_encrypted: str  # Fernet ciphertext
    allowed_domains: list[str] = Field(default_factory=list)
    enforced: bool = False


class VerifiedDomain(BaseModel):
    """One claimed email domain on a workspace (Wave 3 Task 12).

    DNS TXT-record proof: when a record matching ``verification_token``
    is found on the domain, ``verified`` flips True. Once verified +
    ``auto_join``, new registrants with that email domain are routed
    into the workspace as ``member`` by ``UserManager.on_after_register``.
    """

    domain: str  # "acme.com" — lowercase, no @
    verification_token: str  # "paw-verify=<32 hex>"
    verified: bool = False
    verified_at: datetime | None = None
    auto_join: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Workspace(TimestampedDocument):
    """Organization workspace — one per enterprise deployment."""

    name: str
    slug: Indexed(str, unique=True)  # type: ignore[valid-type]
    owner: str  # User ID (admin who created it)
    plan: str = "team"  # from license: team | business | enterprise
    seats: int = 5
    settings: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    sso_config: SsoConfig | None = None
    verified_domains: list[VerifiedDomain] = Field(default_factory=list)
    deleted_at: datetime | None = None

    class Settings:
        name = "workspaces"
