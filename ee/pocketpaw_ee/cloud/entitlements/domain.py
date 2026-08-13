# ee/pocketpaw_ee/cloud/entitlements/domain.py — the frozen, framework-free
# value object the entitlements resolver returns (BC-6, the Entitlement
# primitive).
#
# ``Entitlements`` is the normalized "what is this workspace entitled to" shape:
# its resolved plan key, the plan's feature set, its monthly credit allotment, and
# its monthly credit ceiling (the quota cap). It carries no framework type (no
# Beanie, no FastAPI) so the service can build it and the DTO layer can map it
# without either reaching into the other. It is derived from the EXISTING
# ``Workspace.plan`` field + the billing plan catalog — there is no event
# projection here.
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.
# Updated 2026-06-30 (feat/billing-quota-enforcement, chunk 1): added
#   ``monthly_ceiling: int | None`` next to ``monthly_credit_allotment`` — the
#   per-plan monthly credit CAP (None = uncapped) the resolver populates from the
#   plan catalog and later quota chunks enforce against.
# Updated 2026-07-08 (feat/billing-smb-caps): added ``max_seats`` / ``max_pockets``
#   / ``max_connectors`` (all ``int | None``, None = uncapped) beside
#   ``monthly_ceiling`` — the SMB resource ceilings the resolver populates from the
#   plan catalog and the seat / pocket-create / connector-enable gates enforce
#   against at create time. Fail-closed to the Free value on the fallback path.
# Updated 2026-08-08 (feat/billing-rbac-member-caps): added
#   ``max_call_seconds_per_day`` (``int | None``, None = uncapped) — the daily
#   LiveKit CALL-TIME budget in seconds the LiveKit room-create gate enforces
#   at call-start time. Fail-closed to the Free value (0 = no calls) on the
#   fallback path.
# Updated 2026-08-08 (feat/billing-storage-caps): added ``max_storage_bytes``
#   (``int | None``, None = uncapped) — the workspace S3 STORAGE cap in bytes the
#   uploads pipeline enforces at upload time. Fail-closed to the Free value
#   (5 GB) on the fallback path.
# Updated 2026-08-13 (feat/sites-site-entitlements): added ``SiteEntitlements`` —
#   the PER-SITE shape, a second scope beside the workspace one. Sites are the
#   only thing billed per-object (``Site.plan_tier`` + ``subscription_status``),
#   so "what may this SITE do" cannot be answered by the workspace resolver.
#   Deliberately a separate frozen class rather than fields bolted onto
#   ``Entitlements``: they resolve from different sources, on different cadences,
#   and a caller that wants one almost never wants the other.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entitlements:
    """What a workspace is entitled to, resolved from its current plan.

    ``plan`` is the resolved tier key (matches ``Workspace.plan`` /
    ``PLAN_FEATURES`` — a workspace with no/unknown plan resolves to the base
    ``free`` tier). ``features`` is that tier's feature set (the same set
    ``PLAN_FEATURES`` and the policy gate use — one source of truth).
    ``monthly_credit_allotment`` is integer credits (1 credit == $0.01) granted
    per renewal for the tier. ``monthly_ceiling`` is the per-plan monthly credit
    CAP (integer credits, or None = uncapped) credit-quota enforcement caps spend
    against; a workspace with no/unknown plan resolves to the Free ceiling (the
    fail-closed trial cap), never None/uncapped. ``max_seats`` / ``max_pockets`` /
    ``max_connectors`` are the SMB resource ceilings (integer, or None = uncapped
    for Enterprise) the seat / pocket-create / connector-enable gates enforce at
    create time; a no/unknown-plan workspace resolves to the Free values
    (fail-closed), never None/uncapped. ``max_call_seconds_per_day`` is the daily
    LiveKit CALL-TIME budget in seconds (integer, or None = uncapped for
    Enterprise) the LiveKit room-create gate enforces at call-start time; a
    no/unknown-plan workspace resolves to the Free value (0 = no calls),
    fail-closed, never None/uncapped. ``max_storage_bytes`` is the workspace S3
    STORAGE cap in bytes (integer, or None = uncapped for Enterprise) the
    uploads pipeline enforces at upload time; a no/unknown-plan workspace
    resolves to the Free value (5 GB), fail-closed, never None/uncapped.
    """

    workspace_id: str
    plan: str
    monthly_credit_allotment: int
    monthly_ceiling: int | None
    max_seats: int | None
    max_pockets: int | None
    max_connectors: int | None
    max_call_seconds_per_day: int | None
    max_storage_bytes: int | None
    features: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class SiteEntitlements:
    """What ONE site is entitled to, resolved from its own per-site plan.

    A second scope beside ``Entitlements``. Sites are the only thing this system
    bills per-object, so "may THIS site drop its badge" is not answerable from the
    workspace plan — it depends on ``Site.plan_tier`` and, critically, on whether
    that site's own subscription is actually paying.

    ``subscription_active`` is the load-bearing field and the reason this class
    exists. A cancelled per-site subscription sets ``subscription_status`` and
    LEAVES ``plan_tier`` on the paid key — nothing resets it — so a resolver that
    reads the tier alone hands a cancelled site every paid capability forever.
    The same hole is open wider today: with no Dodo product configured, a paid
    publish records its intended tier with NO live charge and
    ``subscription_status="none"``. Every paid capability below is therefore
    gated on the tier granting it AND the subscription being active.

    Only "pending" is worth a note among the inactive states: a pending site is
    not deployed yet (the charge-first flow deploys on activation), so failing
    closed on it cannot badge a live paying site.

    Deliberately ABSENT: ``conv_allowance``, ``conv_rate_usd`` and
    ``white_label``. The first two wait on which meter owns a concierge run, and
    the third on an org entity that does not exist. A field that always returns
    0/False reads as implemented, which is worse than its absence.
    """

    site_id: str
    workspace_id: str
    plan_tier: str
    subscription_active: bool
    badge_required: bool
    custom_domain: bool
    concierge_enabled: bool
