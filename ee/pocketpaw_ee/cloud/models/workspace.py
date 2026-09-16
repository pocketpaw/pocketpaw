"""Workspace document — one per deployment/org.

2026-09-12 (sites lifecycle wave 3 — transfer): added
``WorkspaceSettings.site_transfers_allowed``. Moving a site to another workspace
takes its leads and concierge transcripts with it, so an admin can forbid the
outbound half outright. Defaults True so every existing workspace reads exactly
as it did before the field existed — no migration, and nothing that worked
yesterday stops. Only the SOURCE side is gated here; the receiving side is
governed by consent (the accept step), not by a setting.

2026-07-10 (compliance-starter): ``WorkspaceSettings.retention_days`` is no
longer decorative. Added a field validator so a persisted value is always
``None`` (keep forever) or a POSITIVE day count — 0 / negative are rejected
at construction, closing both the dedicated retention endpoint and the
general ``update()`` settings-merge path. The setting is read/written through
``workspace.service.get_retention`` / ``set_retention`` and enforced by
``workspace.service.enforce_retention`` (purges audit rows older than the
cutoff). Nothing else on this document changed.

2026-06-28 (AW-7 template gate deny-on-no-match): added
``Workspace.instinct_template_default_deny`` — the PER-WORKSPACE override for
the TEMPLATE-level deny-by-default. ``None`` (the default) means "use the
global config default" (``Settings.instinct_template_default_deny``, itself
False). When set True, a template BOUND to a pocket that declares no rule
matching a MUTATING action parks the write for a human instead of firing;
reads stay ungated. Resolved exactly like ``instinct_approval_level`` (per-
workspace field → global default) via
``resolve_workspace_template_default_deny``; the cloud router reads it and
threads it through ``run_action`` → ``gate_action``.

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

2026-09-16 (Paw Admin chunk 7, Decision 7): added the ``WorkspaceOverrides``
sub-model and a top-level ``Workspace.overrides`` field — a platform
operator's per-tenant entitlement overrides, set/cleared only through
``cloud/platform/entitlements.py`` and applied by
``entitlements.service.resolve_entitlements``. Deliberately does NOT cover
every ``Entitlements`` field: see ``WorkspaceOverrides``'s own docstring for
which two fields were left out and why (PRD errata C2 — both are read by
their enforcement points straight off the plan catalog, never through the
resolver, so an override on either would be stored and displayed while
granting nothing).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from beanie import Indexed
from pydantic import BaseModel, Field, field_validator
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WorkspaceSettings(BaseModel):
    default_agent: str | None = None  # Agent ID
    allow_invites: bool = True
    # Compliance retention policy: None = keep forever; otherwise a POSITIVE
    # number of days after which audit records are purged (enforced by
    # ``workspace.service.enforce_retention``). Zero / negative is rejected so
    # a bad value can never silently disable retention or wipe everything.
    retention_days: int | None = None
    # Whether a site owner may move one of this workspace's sites OUT to another
    # workspace (sites lifecycle wave 3). A transfer carries the site's leads and
    # its concierge transcripts with it, so this is a DATA-EGRESS control and
    # belongs to the workspace rather than to whoever happens to own the site —
    # the same reasoning Netlify applies to its team-level transfer lock.
    #
    # Defaults True, which keeps the feature self-serve for the ordinary case: a
    # workspace that has never thought about this reads as permissive, exactly as
    # it did before the field existed, so there is no migration and nothing that
    # worked yesterday stops. An admin turning it off is making a deliberate
    # choice, and only the SOURCE side is gated — receiving a site is governed by
    # the recipient's own consent, which is the accept step.
    site_transfers_allowed: bool = True

    @field_validator("retention_days")
    @classmethod
    def _validate_retention_days(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(
                "retention_days must be a positive number of days, or null to keep forever"
            )
        return v


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


class WorkspaceOverrides(BaseModel):
    """Platform-operator entitlement overrides (Paw Admin chunk 7, Decision 7).

    Each field is independently tri-state: ``None`` means "not overridden — use
    the plan catalog's value", the literal string ``"uncapped"`` means
    "override to no limit", and an int means "override to exactly this limit".
    Three states because a plain ``int | None`` cannot tell "leave the catalog
    alone" apart from "override to unlimited" — both would collapse onto the
    same wire value (``null``) otherwise, and an operator lifting Free's
    ``max_seats=0`` to uncapped needs to say that, not merely "no opinion".

    Every field here is read by ``resolve_entitlements``, which is the single
    choke point every enforcement path in the codebase calls through — an
    override set here reaches all of them with no extra plumbing. That
    includes ``max_call_seconds_per_day``, which Decision 7's original field
    list omitted despite it already being resolver-enforced (PRD errata C2).

    Deliberately does NOT model ``monthly_credit_allotment`` or
    ``extra_features``. Verified against every consumer in the tree, not just
    ``Entitlements``: the credit-renewal grant reads
    ``plan_catalog.get_plan(event.plan_key).monthly_credit_allotment`` directly
    (``billing/service.py``), and both ``require_plan_feature`` implementations
    (``cloud/_core/deps.py``, ``guards/deps.py``) test
    ``feature in PLAN_FEATURES.get(plan, set())`` directly — neither goes
    through the resolver. An override on either field would be stored, shown
    on the console, and change nothing, which the PRD errata calls worse than
    the field not existing: the ticket would read as fixed when it is not.
    Add either field here only once its underlying gate reads the resolver.

    ``expires_at`` — ``None`` never expires. Once past, the resolver treats
    every field on this document as absent, not just the ones past their own
    clock — a partial expiry would leave an operator unable to reason about
    what is still in effect.
    """

    monthly_ceiling: int | Literal["uncapped"] | None = None
    max_seats: int | Literal["uncapped"] | None = None
    max_pockets: int | Literal["uncapped"] | None = None
    max_connectors: int | Literal["uncapped"] | None = None
    max_call_seconds_per_day: int | Literal["uncapped"] | None = None
    max_storage_bytes: int | Literal["uncapped"] | None = None
    included_sites: int | Literal["uncapped"] | None = None
    expires_at: datetime | None = None

    @field_validator(
        "monthly_ceiling",
        "max_seats",
        "max_pockets",
        "max_connectors",
        "max_call_seconds_per_day",
        "max_storage_bytes",
        "included_sites",
    )
    @classmethod
    def _validate_non_negative(cls, v: int | str | None) -> int | str | None:
        if isinstance(v, int) and v < 0:
            raise ValueError("override value must be zero or positive, or the string 'uncapped'")
        return v


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
    # Platform-operator entitlement overrides (Paw Admin chunk 7). None = no
    # override in effect; set/cleared only by the platform write route in
    # ``cloud/platform/entitlements.py`` (via its cross-tenant helper in
    # ``workspace/service.py``), never by the tenant's own admin. See
    # ``WorkspaceOverrides`` for the field list and why two ``Entitlements``
    # fields are deliberately absent from it.
    overrides: WorkspaceOverrides | None = None
    sso_config: SsoConfig | None = None
    verified_domains: list[VerifiedDomain] = Field(default_factory=list)
    deleted_at: datetime | None = None
    # Per-member route-level permissions: user_id → list of allowed route keys.
    # An empty list or missing entry means the user has full access (no restrictions).
    route_permissions: dict[str, list[str]] = Field(default_factory=dict)
    # Per-member connector-level permissions: user_id → list of allowed connector names.
    # An empty list or missing entry means the user has full access (no restrictions).
    connector_permissions: dict[str, list[str]] = Field(default_factory=dict)
    # Per-member granular action overrides: user_id → list of action keys
    # (e.g. "channel.create") granted beyond the member's base workspace role.
    # Only keys in OVERRIDABLE_ACTIONS are valid. An empty list or missing
    # entry means the member's permissions match their role defaults.
    action_permissions: dict[str, list[str]] = Field(default_factory=dict)
    # Layered/learning Instinct gate (T6) — per-workspace triager activation
    # level. None = use the global config default (Settings.
    # instinct_approval_level, "ASK"). A workspace owner opts in to "TRIAGE"
    # (or future "TRUSTED") to activate the auto/optimistic lanes for THIS
    # workspace's writes; nothing else changes the default for an existing
    # tenant (design MF-9 — global config cannot silently upgrade tenants).
    instinct_approval_level: str | None = None
    # AW-7 — per-workspace override for the TEMPLATE-level deny-by-default.
    # None = use the global config default (Settings.
    # instinct_template_default_deny, False). True parks a MUTATING action a
    # bound template declares no rule for (instead of firing); reads stay
    # ungated. Same MF-9 contract as instinct_approval_level above — a global
    # env var never silently flips an existing tenant.
    instinct_template_default_deny: bool | None = None

    class Settings:
        name = "workspaces"
        # Added 2026-09-14 for the Paw Admin tenant directory. Until now the
        # only index on this collection was the unique one Beanie derives from
        # ``slug``, because every read was "the workspaces this user belongs
        # to" — resolved from the USER document, which never touches this
        # collection's predicates at all.
        #
        # The operator console inverts that: it lists and filters across all
        # tenants, so these predicates run against the whole collection for the
        # first time and each one was a COLLSCAN.
        indexes = [
            # The default directory listing: not-deleted, newest first. Sorting
            # is by _id (monotonic, unique) rather than createdAt, so the
            # cursor cannot skip a row or repeat one when two workspaces land
            # in the same clock tick.
            IndexModel([("deleted_at", 1), ("_id", -1)], name="deleted_at_1__id_-1"),
            # "Which workspaces does this user own", and the owner-email search
            # path, which resolves emails to ids and then matches $in here.
            IndexModel([("owner", 1)], name="owner_1"),
            # Plan filter, and the plan-mix aggregate the dashboard will read.
            IndexModel([("plan", 1)], name="plan_1"),
            # Name search. A case-insensitive $regex can only use this for an
            # anchored prefix, so a mid-string match still scans — acceptable
            # while the tenant count is small, and the honest fix later is a
            # text index or a normalised lowercase field, not a bigger regex.
            IndexModel([("name", 1)], name="name_1"),
        ]
