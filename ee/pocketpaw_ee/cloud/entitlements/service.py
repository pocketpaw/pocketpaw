# ee/pocketpaw_ee/cloud/entitlements/service.py — the entitlements RESOLVER
# (BC-6, the Entitlement primitive).
#
# Module-level ``async def`` API (NOT a class, per EE cloud rule, mirroring
# ``credits.service`` / ``billing.service``). Public API:
#   * ``resolve_entitlements(workspace_id)`` — read the workspace's CURRENT plan
#     (``workspace.service.get_workspace_plan``), look it up in the billing plan
#     catalog (``billing.plans``), and return an ``Entitlements`` (plan +
#     features + monthly credit allotment + monthly credit ceiling).
#
# READ-ONLY: no writes, no emit (EE cloud rule 9 only fires on mutation; this
# entity mutates nothing). Tenancy: ``get_workspace_plan`` resolves the plan from
# the ONE ``Workspace`` document with that id and returns None for a missing /
# soft-deleted / malformed id, so there is no cross-tenant read here — a caller
# only ever sees the plan of the workspace it asked for.
#
# FALLBACK: a workspace with no plan, an unknown plan string, or a missing
# workspace resolves to the ``free`` base tier (``plans.BASE_PLAN_KEY``) — never
# a crash, never a silent upgrade to a paid tier. The Free tier carries the
# explicit 1000 credit ceiling, so the fallback also fails closed on the quota cap
# (never None/uncapped). (Subscription EVENTS that CHANGE the plan are BC-7's job;
# here entitlements derive from the existing ``Workspace.plan`` field as it stands.)
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.
# Updated 2026-06-30 (feat/billing-quota-enforcement, chunk 1): ``Entitlements``
#   now also carries ``monthly_ceiling`` — populated from the resolved tier's
#   ``monthly_ceiling`` exactly as ``monthly_credit_allotment`` is. The defensive
#   base-floor branch sets the Free trial ceiling (1000), so every path fails
#   closed and no path leaves the cap uncapped.
# Updated 2026-07-08 (feat/billing-smb-caps): ``Entitlements`` now also carries the
#   three SMB caps (``max_seats`` / ``max_pockets`` / ``max_connectors``), populated
#   from the resolved tier exactly as ``monthly_ceiling`` is. The defensive
#   base-floor branch sets the Free values (5 / 200 / 50) so every path fails closed.
# Updated 2026-08-08 (feat/billing-rbac-member-caps): the Free base-floor
#   ``max_seats`` is now 0 — a workspace with no/unknown plan resolves to the Free
#   tier, which cannot invite ANY members (Paw Go = 5, Paw Pro = 25; Pro Max and
#   Enterprise = None). Fails closed to the most restrictive tier. Also added
#   ``max_call_seconds_per_day`` — the daily LiveKit call budget (Free = 0 → no
#   calls) surfaced to the LiveKit room-create gate; fail-closed to 0.
# Updated 2026-08-08 (feat/billing-storage-caps): also added
#   ``max_storage_bytes`` — the workspace S3 storage cap (Free = 5 GB) surfaced
#   to the uploads gate and the /storage/usage read; fail-closed to 5 GB.

from __future__ import annotations

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing import plans as plan_catalog
from pocketpaw_ee.cloud.billing import site_plans as site_plan_catalog
from pocketpaw_ee.cloud.entitlements.domain import Entitlements, SiteEntitlements


async def resolve_entitlements(workspace_id: str) -> Entitlements:
    """Resolve a workspace to its entitlements (plan + features + allotment).

    Reads the workspace's CURRENT ``Workspace.plan`` and looks the tier up in the
    billing plan catalog. A workspace with no/unknown plan (or one that doesn't
    exist) resolves to the ``free`` base tier — never a crash, never a paid-tier
    leak.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("entitlements.invalid_workspace", "workspace_id is required")

    # Lazy import keeps this module free of the heavy workspace.service import at
    # module load (it pulls Beanie), mirroring how the plan-feature gate dep
    # imports the workspace service inside the guard.
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    plan_key = await workspace_service.get_workspace_plan(workspace_id)

    # None (missing/deleted/malformed id) OR a plan string not in the catalog
    # (a stale/typo'd tier) both fall back to the base floor. ``get_plan``
    # returns None for an unknown key, so this one branch covers both.
    tier = plan_catalog.get_plan(plan_key)
    if tier is None:
        tier = plan_catalog.get_plan(plan_catalog.BASE_PLAN_KEY)
        # The base tier is a static catalog entry; this is never None in
        # practice, but guard so a future catalog edit can't NPE the resolver.
        if tier is None:  # pragma: no cover - defensive; base tier always exists
            return Entitlements(
                workspace_id=workspace_id,
                plan=plan_catalog.BASE_PLAN_KEY,
                monthly_credit_allotment=0,
                # Fail closed: the Free trial cap, never None/uncapped — even when
                # the catalog itself is somehow missing the base tier.
                monthly_ceiling=1_000,
                # Fail closed on the SMB caps too: the Free values (max_seats = 0
                # → a fallback workspace cannot invite any members; call budget 0
                # → no LiveKit calls; storage = 5 GB), never uncapped.
                max_seats=0,
                max_pockets=200,
                max_connectors=50,
                max_call_seconds_per_day=0,
                max_storage_bytes=5_000_000_000,
                features=frozenset(),
            )

    return Entitlements(
        workspace_id=workspace_id,
        plan=tier.key,
        monthly_credit_allotment=tier.monthly_credit_allotment,
        monthly_ceiling=tier.monthly_ceiling,
        max_seats=tier.max_seats,
        max_pockets=tier.max_pockets,
        max_connectors=tier.max_connectors,
        max_call_seconds_per_day=tier.max_call_seconds_per_day,
        max_storage_bytes=tier.max_storage_bytes,
        features=tier.features,
    )


# The per-site subscription states that count as PAYING. Everything else —
# ``none`` (no subscription, including the paid-tier-recorded-but-never-charged
# case a missing Dodo product produces), ``pending`` (created, not yet confirmed;
# such a site is not deployed yet) and ``cancelled`` (which LEAVES ``plan_tier``
# on the paid key) — resolves to no paid capability.
_ACTIVE_SITE_SUBSCRIPTION_STATUSES = frozenset({"active"})


def resolve_site_entitlements(
    *,
    site_id: str,
    workspace_id: str,
    plan_tier: str | None,
    subscription_status: str | None,
    concierge_enabled: bool,
) -> SiteEntitlements:
    """Resolve ONE site to what it may do, from its own per-site plan.

    PURE and synchronous, taking the site's billing fields rather than reading
    them: ``entitlements`` may not import ``models.site`` (EE cloud rule 2 — only
    ``sites/service.py`` owns that document), so the caller that owns the doc
    passes what it owns. That also makes every branch here testable without a
    database.

    Every paid capability is gated on the tier granting it AND the subscription
    being active. Reading the tier alone is the bug this function exists to
    prevent: cancellation never resets ``plan_tier``, and an unconfigured Dodo
    product records a paid tier with no charge at all.

    Fails closed on every unknown: an absent/unknown tier resolves to the base
    (badged, no custom domain) rather than raising or substituting a paid tier.
    """
    tier = site_plan_catalog.get_site_plan(plan_tier)
    # ``get_site_plan`` deliberately does not substitute a floor, so an unknown or
    # missing key lands here as None — the fail-closed default.
    resolved_key = tier.key if tier is not None else site_plan_catalog.BASE_SITE_PLAN_KEY

    subscription_active = (subscription_status or "none") in _ACTIVE_SITE_SUBSCRIPTION_STATUSES

    # Both paid capabilities collapse to False unless the tier grants them AND the
    # subscription is paying. Written as an explicit branch rather than
    # ``paid and tier.x`` so the None-narrowing is visible to the type checker
    # instead of resting on short-circuit evaluation.
    badge_removal = False
    custom_domain = False
    if tier is not None and subscription_active:
        badge_removal = tier.badge_removal
        custom_domain = "custom_domain" in tier.cloudflare_features

    return SiteEntitlements(
        site_id=site_id,
        workspace_id=workspace_id,
        plan_tier=resolved_key,
        subscription_active=subscription_active,
        badge_required=not badge_removal,
        custom_domain=custom_domain,
        concierge_enabled=bool(concierge_enabled),
    )
