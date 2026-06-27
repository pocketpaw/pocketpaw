"""Workspace document — one per deployment/org.

2026-06-19 (layered/learning gate, T6): added
``Workspace.instinct_approval_level`` — the PER-WORKSPACE override for the
layered Instinct gate's triager activation level ("ASK" | "TRIAGE" |
"TRUSTED"). ``None`` (the default) means "use the global config default"
(``Settings.instinct_approval_level``, itself "ASK"). A workspace must
explicitly set this to a non-ASK value to activate auto/optimistic/dry-run
lanes for its writes — a global env var changes the default for NEW
workspaces only and can never silently upgrade an existing tenant (design
MF-9). The cloud router reads this field and passes the resolved level to
``run_action`` → ``gate_action``.

2026-06-14 (WB-1): added the ``Branding`` sub-model and a top-level
``Workspace.branding`` field for white-label theming (logo, display name,
tab title, accent color, favicon, paw-mark toggle). Branding is a per-tenant
IDENTITY field — kept separate from ``WorkspaceSettings`` (operational config)
on purpose. Every sub-field is optional; an unset field falls back to the Paw
default at render time (a frontend concern, not stored here).
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WorkspaceSettings(BaseModel):
    default_agent: str | None = None  # Agent ID
    allow_invites: bool = True
    retention_days: int | None = None  # None = keep forever


class Branding(BaseModel):
    """Per-tenant white-label branding (WB-1).

    All fields optional; each unset field falls back to the Paw default at
    render time (a frontend concern). Asset fields hold an uploaded
    ``FileUpload.file_id`` that must belong to the same workspace — the
    service enforces that ownership before persisting.
    """

    logo_asset: str | None = None  # uploaded asset id — top-bar mark
    favicon_asset: str | None = None  # uploaded asset id — browser favicon
    display_name: str | None = None  # replaces the "PocketPaw" wordmark
    tab_title: str | None = None  # browser tab title
    accent_color: str | None = None  # hex "#RRGGBB" — tints the UI theme
    show_paw_mark: bool = True  # keep/hide our paw icon


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
    plan: str = "free"  # consumer ladder: free | go | pro | pro_max | enterprise
    seats: int = 5
    settings: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    # Per-tenant white-label branding (WB-1). Top-level identity field, NOT
    # nested under settings (which holds operational config). None = no
    # custom branding; the frontend renders the Paw defaults.
    branding: Branding | None = None
    sso_config: SsoConfig | None = None
    verified_domains: list[VerifiedDomain] = Field(default_factory=list)
    deleted_at: datetime | None = None
    # Per-member route-level permissions: user_id → list of allowed route keys.
    # An empty list or missing entry means the user has full access (no restrictions).
    route_permissions: dict[str, list[str]] = Field(default_factory=dict)
    # Per-member connector-level permissions: user_id → list of allowed connector names.
    # An empty list or missing entry means the user has full access (no restrictions).
    connector_permissions: dict[str, list[str]] = Field(default_factory=dict)
    # Layered/learning Instinct gate (T6) — per-workspace triager activation
    # level. None = use the global config default (Settings.
    # instinct_approval_level, "ASK"). A workspace owner opts in to "TRIAGE"
    # (or future "TRUSTED") to activate the auto/optimistic lanes for THIS
    # workspace's writes; nothing else changes the default for an existing
    # tenant (design MF-9 — global config cannot silently upgrade tenants).
    instinct_approval_level: str | None = None

    class Settings:
        name = "workspaces"
