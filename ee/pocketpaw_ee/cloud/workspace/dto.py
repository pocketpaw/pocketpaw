"""Wire DTOs for the workspace domain.

Replaces ``ee/cloud/workspace/schemas.py``. Field names match the
existing wire shape consumed by paw-enterprise:
- ``_id`` (not ``id``) for entity identifiers
- ``createdAt``, ``expiresAt``, ``joinedAt`` (camelCase) for timestamps
- ``memberCount``, ``invitedBy`` (camelCase) for derived/foreign refs
- ``workspace_name``, ``valid`` for the validate-invite response

Changes: added the slug rule single-source-of-truth (``SLUG_RE`` +
``RESERVED_SLUGS``) and the ``SlugAvailabilityOut`` response so the create
UI can check a slug live; ``validate_slug`` now reuses ``SLUG_RE``.
2026-06-14 (WB-1): added ``BrandingPatch`` (request, with ``accent_color``
format validation) + ``BrandingOut`` (response), an optional ``branding``
field on ``UpdateWorkspaceRequest``, a ``branding`` field on ``WorkspaceOut``,
and the ``Branding`` -> ``BrandingOut`` mapping in ``workspace_to_dto``.
2026-06-19 (feat/instinct-gate-integration, security-review FIX 1): added
``SetApprovalLevelRequest`` — the closed-enum body for the OWNER-only route
that activates the layered Instinct gate's triager for a workspace.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.workspace.domain import (
    Branding,
    Invite,
    InviteContext,
    VerifiedDomain,
    Workspace,
    WorkspaceMember,
)
from pocketpaw_ee.cloud.workspace.slug import SLUG_RE

# Hex accent color, e.g. "#1A2B3C". Anchored — rejects "blue", "#ZZZ",
# short/long hex, and a missing leading "#".
ACCENT_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# ---------------------------------------------------------------------------
# Requests (preserved from schemas.py)
# ---------------------------------------------------------------------------


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("Slug must be lowercase alphanumeric with hyphens")
        return v


class BrandingPatch(BaseModel):
    """Per-tenant white-label branding patch (WB-1), carried on
    ``UpdateWorkspaceRequest.branding``.

    All fields optional. ``accent_color`` is format-validated here (must be
    ``#RRGGBB``) so a malformed value is a 422 at the route boundary, before
    the service runs. Asset ownership (``logo_asset`` / ``favicon_asset``
    must belong to the target workspace) is a DB-backed check enforced in the
    service, not here."""

    logo_asset: str | None = None
    favicon_asset: str | None = None
    display_name: str | None = None
    tab_title: str | None = None
    accent_color: str | None = None
    show_paw_mark: bool = True

    @field_validator("accent_color")
    @classmethod
    def validate_accent_color(cls, v: str | None) -> str | None:
        if v is not None and not ACCENT_COLOR_RE.match(v):
            raise ValueError("accent_color must be a hex color like #RRGGBB")
        return v


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    settings: dict | None = None
    # Per-tenant white-label branding (WB-1). Owner/admin-gated at the route
    # via the same ``workspace.update`` action as the rename path.
    branding: BrandingPatch | None = None


class InviteContextDTO(BaseModel):
    """Optional admin-provided onboarding hints carried on an invite (pp#1365).

    The same ``{focus, profile_pic}`` shape on both the create request and the
    invite response. ``focus`` is a one-line description of what the new member
    will own; ``profile_pic`` is a reference (e.g. an uploaded file id) to a
    suggested avatar. Both optional — supply either, both, or neither. Consumed
    by the downstream VIP-onboarding flow."""

    focus: str | None = Field(default=None, max_length=280)
    profile_pic: str | None = Field(default=None, max_length=512)


class CreateInviteRequest(BaseModel):
    email: str
    role: str = Field(default="member", pattern="^(admin|member)$")
    group_id: str | None = None
    context: InviteContextDTO | None = None


class UpdateMemberRoleRequest(BaseModel):
    role: str = Field(pattern="^(owner|admin|member)$")


class SetApprovalLevelRequest(BaseModel):
    """PATCH /workspaces/{id}/instinct/approval-level request (security FIX 1).

    The body for the OWNER-only switch that activates the layered Instinct
    gate's triager for a workspace. ``level`` is a closed enum
    (``ASK`` | ``TRIAGE`` | ``TRUSTED``) so the route boundary 422s on any
    other value — a typo can never silently land a junk level on the workspace
    document (which the gate would then read and route ASK on, masking the
    misconfiguration). ``extra="forbid"`` rejects stray fields. The service
    re-validates against the canonical ``ApprovalLevel`` enum (defense in
    depth, and so direct service callers get the same guarantee)."""

    model_config = ConfigDict(extra="forbid")

    level: Literal["ASK", "TRIAGE", "TRUSTED"]


class BulkInviteRequest(BaseModel):
    """POST /workspaces/{id}/invites/bulk request.

    ``emails`` is bounded at 100 so a single batch can't dwarf the daily
    invite-rate budget. The frontend's paste-a-list UI clamps client-side.
    """

    emails: list[EmailStr] = Field(min_length=1, max_length=100)
    role: str = Field(default="member", pattern="^(admin|member)$")
    group_id: str | None = None


class BulkInviteSkip(BaseModel):
    """One per-email skip in the bulk response."""

    email: str
    reason: Literal["already_member", "already_pending", "invalid_email", "seat_limit"]


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class BrandingOut(BaseModel):
    """Per-tenant white-label branding on the workspace read (WB-1).

    Mirrors ``BrandingPatch`` minus the validators — it's a read shape. The
    frontend applies the Paw default for any field that's ``None``."""

    logo_asset: str | None = None
    favicon_asset: str | None = None
    display_name: str | None = None
    tab_title: str | None = None
    accent_color: str | None = None
    show_paw_mark: bool = True


class WorkspaceOut(BaseModel):
    """GET /workspaces/{id} response.

    Also the shape the shell reads at load for the active workspace (there is
    no separate ``/workspaces/active`` route — the shell fetches the active
    workspace via ``GET /workspaces/{id}`` using the id from the profile), so
    ``branding`` here is enough for the shell to theme on first paint."""

    id: str = Field(serialization_alias="_id")
    name: str
    slug: str
    owner: str
    plan: str
    seats: int
    createdAt: str | None  # noqa: N815 - camelCase wire key
    memberCount: int  # noqa: N815 - camelCase wire key
    branding: BrandingOut | None = None  # per-tenant white-label branding (WB-1)

    model_config = {"populate_by_name": True}


class SlugAvailabilityOut(BaseModel):
    """GET /workspaces/slug-available response.

    ``available`` is the headline; ``reason`` names *why* an unavailable slug
    is unavailable (``invalid`` format, ``reserved`` handle, or already
    ``taken``) so the create UI can show a precise message live instead of
    waiting for the POST to 409.
    """

    available: bool
    reason: Literal["invalid", "reserved", "taken"] | None = None


class MemberOut(BaseModel):
    """A member entry returned by GET /workspaces/{id}/members."""

    id: str = Field(serialization_alias="_id")
    email: str
    name: str
    avatar: str
    role: str
    joinedAt: str | None  # noqa: N815 - camelCase wire key

    model_config = {"populate_by_name": True}


class InviteOut(BaseModel):
    """An invite entry."""

    id: str = Field(serialization_alias="_id")
    email: str
    role: str
    invitedBy: str  # noqa: N815 - camelCase wire key
    token: str | None = None
    accepted: bool
    revoked: bool
    expired: bool
    expiresAt: str | None  # noqa: N815 - camelCase wire key
    context: InviteContextDTO | None = None  # optional admin onboarding hints (pp#1365)

    model_config = {"populate_by_name": True}


class BulkInviteResponse(BaseModel):
    """POST /workspaces/{id}/invites/bulk response."""

    created: list[InviteOut]
    skipped: list[BulkInviteSkip]


class ValidateInviteOut(InviteOut):
    """GET /workspaces/invites/{token} response. Adds ``valid`` and
    ``workspace_name`` for the frontend's invite-landing page."""

    valid: bool
    workspace_name: str


class WorkspaceDeletePreviewResponse(BaseModel):
    """GET /workspaces/{id}/delete-preview response — blast-radius before delete.

    Counts the rows the cascade in ``workspace_service.delete`` will tear
    through (members, chat groups, agents, files, pending invites) plus the
    total file bytes attributable to the workspace. The UI uses this for a
    "Deleting will remove X members, Y rooms, Z bytes — this cannot be
    undone" confirmation step before the type-name-to-confirm prompt.
    """

    member_count: int
    room_count: int
    agent_count: int
    file_count: int
    invite_count: int
    total_bytes: int


class AddDomainRequest(BaseModel):
    """POST /workspaces/{id}/domains."""

    domain: str = Field(min_length=3, max_length=253)


class UpdateDomainRequest(BaseModel):
    """PATCH /workspaces/{id}/domains/{domain}."""

    auto_join: bool


class VerifiedDomainOut(BaseModel):
    """One verified-domain entry. ``verification_token`` is the value the
    admin must place in the domain's DNS TXT record before calling verify."""

    domain: str
    verification_token: str
    verified: bool
    verified_at: str | None = None
    auto_join: bool
    created_at: str | None = None


class RoutePermissionsOut(BaseModel):
    """GET /workspaces/{id}/route-permissions response.

    A map of user_id → list of allowed route keys. An empty list or missing
    entry means the user has full access (no route restrictions).
    """

    permissions: dict[str, list[str]]


class SetMemberRoutePermissionsRequest(BaseModel):
    """PUT /workspaces/{id}/route-permissions/{user_id} request.

    An empty routes list grants full access (clears restrictions).
    """

    routes: list[str] = Field(default_factory=list)

    @field_validator("routes")
    @classmethod
    def validate_routes(cls, v: list[str]) -> list[str]:
        VALID_ROUTES = {
            "studio",
            "chat",
            "code",
            "agents",
            "pockets",
            "deep-work",
            "calendar",
            "activity",
            "files",
            "meetings",
            "mission-control",
            "knowledge",
            "decisions-graph",
            "foresight",
            "sites",
            "belt",
            "paw-print",
            "audit",
            "settings",
        }
        for route in v:
            if route not in VALID_ROUTES:
                raise ValueError(f"Invalid route key: {route}")
        return v


class ConnectorPermissionsOut(BaseModel):
    """GET /workspaces/{id}/connector-permissions response.

    A map of user_id → list of allowed connector names. An empty list or
    missing entry means the user has full access (no connector restrictions).
    """

    permissions: dict[str, list[str]]


class SetMemberConnectorPermissionsRequest(BaseModel):
    """PUT /workspaces/{id}/connector-permissions/{user_id} request.

    An empty connectors list grants full access (clears restrictions).
    """

    connectors: list[str] = Field(default_factory=list)


class InvitePreviewResponse(BaseModel):
    """Typed preview of an invite token for the accept UI.

    ``state`` is the single field the UI switches on:
      - ``ready_new``         — token is valid, viewer is anonymous; show register form
      - ``ready_existing``    — token is valid, viewer logged in with matching email
      - ``ready_wrong_user``  — token is valid, viewer logged in with a DIFFERENT email
      - ``expired``           — token expired
      - ``revoked``           — token revoked by inviter
      - ``already_accepted``  — token already redeemed
      - ``not_found``         — token doesn't exist (or was tampered)
    """

    state: Literal[
        "ready_new",
        "ready_existing",
        "ready_wrong_user",
        "expired",
        "revoked",
        "already_accepted",
        "not_found",
    ]
    email: str | None = None
    role: str | None = None
    workspace_name: str | None = None
    group: str | None = None
    group_name: str | None = None
    viewer_email: str | None = None
    # Optional admin onboarding hints (pp#1365), surfaced on ready_* previews so
    # the member-facing accept UI can carry focus + profile_pic into the
    # downstream VIP-onboarding welcome. None/absent when the invite has none.
    context: InviteContextDTO | None = None


def _branding_to_dto(b: Branding | None) -> BrandingOut | None:
    if b is None:
        return None
    return BrandingOut(
        logo_asset=b.logo_asset,
        favicon_asset=b.favicon_asset,
        display_name=b.display_name,
        tab_title=b.tab_title,
        accent_color=b.accent_color,
        show_paw_mark=b.show_paw_mark,
    )


def workspace_to_dto(ws: Workspace) -> WorkspaceOut:
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        owner=ws.owner,
        plan=ws.plan,
        seats=ws.seats,
        createdAt=iso_utc(ws.created_at),
        memberCount=ws.member_count,
        branding=_branding_to_dto(ws.branding),
    )


def member_to_dto(m: WorkspaceMember) -> MemberOut:
    return MemberOut(
        id=m.user_id,
        email=m.email,
        name=m.name,
        avatar=m.avatar,
        role=m.role,
        joinedAt=iso_utc(m.joined_at),
    )


def _context_to_dto(ctx: InviteContext | None) -> InviteContextDTO | None:
    if ctx is None:
        return None
    return InviteContextDTO(focus=ctx.focus, profile_pic=ctx.profile_pic)


def invite_to_dto(inv: Invite) -> InviteOut:
    return InviteOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        invitedBy=inv.invited_by,
        token=inv.token,
        accepted=inv.accepted,
        revoked=inv.revoked,
        expired=inv.expired,
        expiresAt=iso_utc(inv.expires_at),
        context=_context_to_dto(inv.context),
    )


def verified_domain_to_dto(d: VerifiedDomain) -> VerifiedDomainOut:
    return VerifiedDomainOut(
        domain=d.domain,
        verification_token=d.verification_token,
        verified=d.verified,
        verified_at=iso_utc(d.verified_at),
        auto_join=d.auto_join,
        created_at=iso_utc(d.created_at),
    )


def invite_to_validate_dto(inv: Invite, workspace_name: str) -> ValidateInviteOut:
    return ValidateInviteOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        invitedBy=inv.invited_by,
        token=inv.token,
        accepted=inv.accepted,
        revoked=inv.revoked,
        expired=inv.expired,
        expiresAt=iso_utc(inv.expires_at),
        context=_context_to_dto(inv.context),
        valid=not (inv.accepted or inv.revoked or inv.expired),
        workspace_name=workspace_name,
    )


__all__ = [
    "AddDomainRequest",
    "BrandingOut",
    "BrandingPatch",
    "BulkInviteRequest",
    "BulkInviteResponse",
    "BulkInviteSkip",
    "ConnectorPermissionsOut",
    "CreateInviteRequest",
    "CreateWorkspaceRequest",
    "InviteContextDTO",
    "InviteOut",
    "InvitePreviewResponse",
    "MemberOut",
    "RoutePermissionsOut",
    "SetMemberConnectorPermissionsRequest",
    "SetMemberRoutePermissionsRequest",
    "SlugAvailabilityOut",
    "UpdateDomainRequest",
    "UpdateMemberRoleRequest",
    "UpdateWorkspaceRequest",
    "ValidateInviteOut",
    "VerifiedDomainOut",
    "WorkspaceDeletePreviewResponse",
    "WorkspaceOut",
    "invite_to_dto",
    "invite_to_validate_dto",
    "member_to_dto",
    "verified_domain_to_dto",
    "workspace_to_dto",
]
