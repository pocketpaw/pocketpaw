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
# Updated 2026-08-21 (feat/site-free-custom-domain, PW-1): ``resolve_site_entitlements``
#   no longer has ONE branch. It has two, and the split is the point of the change:
#   PAID grants (badge removal, concierge, an UNCAPPED domain allowance) still need
#   an active subscription, while the FLOOR grant (``max_domained_sites``) resolves
#   off the base tier whether or not anyone is paying — because free now includes a
#   custom domain, and a catalog edit alone could never have delivered one. Under
#   the old single branch every $0 tier fell through to the all-False defaults, so a
#   floor capability was structurally unexpressible. Also extracted the
#   active-subscription test to ``_subscription_is_active`` now that two branches
#   ask it.
# Updated 2026-08-20 (feat/site-plan-catalog-inclusions): ``concierge_entitled``
#   now reads ``tier.sells_concierge`` off the catalog row instead of re-deriving
#   "above the free floor" here — the plan-catalog DTO needs the same answer for
#   the buyer-facing plan cards, and two copies of one rule drift. The AND with an
#   active subscription stays here; that is this resolver's job, not the catalog's.
# Updated 2026-08-22 (feat/site-pricing-ladder): both per-site reads
#   (``site_domain_allowance`` and ``resolve_site_entitlements``) now go through
#   ``site_plans.site_scoped_tier`` instead of ``get_site_plan``. The catalog gained
#   ORG-scoped flats (studio/agency) in the pricing rekey, and their keys are not
#   legal ``Site.plan_tier`` values — a plain lookup would resolve one off a single
#   site's field and grant that site an allowance the org buys once for many.
#   ``site_scoped_tier`` returns None for them, so an org key on a site fails closed
#   to the free floor exactly as an unknown key does. The same call also resolves
#   the LEGACY basic/pro/business keys, which is what stops the rekey demoting every
#   already-published site the day it deploys.

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


def _subscription_is_active(subscription_status: str | None) -> bool:
    """Is this site's per-site subscription actually paying?

    Extracted from ``resolve_site_entitlements``'s single branch when that branch
    became two (floor grants vs paid grants) and both needed the same answer. A
    None / empty status normalizes to "none" — absent is not paying.
    """
    return (subscription_status or "none") in _ACTIVE_SITE_SUBSCRIPTION_STATUSES


def site_domain_allowance(*, plan_tier: str | None, subscription_status: str | None) -> int | None:
    """How many SITES may carry a custom domain, from THIS site's own plan.

    ``None`` means uncapped. Public because the ATTACH seam needs it per row: to
    decide whether a workspace has room for one more domained site it has to ask,
    of every site already holding a domain, whether that site is riding the free
    floor or paying for its own uncapped allowance. Only a site on the floor spends
    the workspace's floor allowance.

    Split out of ``resolve_site_entitlements`` rather than re-derived there, so the
    floor-vs-paid rule is written once. ``sites.service`` calling this is not a
    layering break: it is a pure function of two strings, which is the same reason
    ``resolve_site_entitlements`` takes the site's billing fields instead of
    reading them (EE cloud rule 2).
    """
    # The floor first — it applies to an unknown tier, an absent tier, and a paid
    # tier whose subscription has lapsed, all of which must land on the same
    # answer. Free includes one domained site, so this is a grant, not a denial.
    floor = site_plan_catalog.get_site_plan(site_plan_catalog.BASE_SITE_PLAN_KEY)
    allowance = floor.max_domained_sites if floor is not None else 0

    # A paying tier's own allowance REPLACES the floor — normally upward
    # (None = uncapped). Not ``max(...)``: None is not a number, and a tier that
    # deliberately sells fewer domained sites than free should be able to.
    #
    # ``site_scoped_tier`` and not ``get_site_plan``: the catalog now also holds
    # ORG flats (studio/agency), whose keys are not legal ``Site.plan_tier``
    # values. Resolving one here would read an org-wide allowance off a single
    # site's field. It returns None for those, which lands on the floor.
    tier = site_plan_catalog.site_scoped_tier(plan_tier)
    if tier is not None and _subscription_is_active(subscription_status):
        allowance = tier.max_domained_sites
    return allowance


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

    PAID capabilities are gated on the tier granting it AND the subscription being
    active. Reading the tier alone is the bug this function exists to prevent:
    cancellation never resets ``plan_tier``, and an unconfigured Dodo product
    records a paid tier with no charge at all.

    FLOOR capabilities are the exception, and ``max_domained_sites`` is the first
    of them. Free includes one domained site, so that allowance has to resolve with
    no subscription — the base tier confers it, and an active paid subscription
    only ever REPLACES it. Before this split there was one branch and every $0 tier
    fell straight through it to all-False, which made a floor capability impossible
    to express in the catalog at all.

    Fails closed on every unknown: an absent/unknown tier resolves to the base
    (badged, and the base tier's own domain allowance) rather than raising or
    substituting a paid tier.
    """
    # ``site_scoped_tier`` rather than ``get_site_plan``, and the difference is a
    # security property rather than a tidiness one. The catalog now carries ORG
    # flats (studio/agency) beside the per-site rungs, and an org key is not a
    # legal ``Site.plan_tier``. A plain catalog lookup would resolve one and hand
    # THIS SITE the badge removal and white-label allowance an org pays for across
    # twenty-five. ``site_scoped_tier`` returns None for them, which is the same
    # fail-closed answer an unknown key gets: the free floor.
    tier = site_plan_catalog.site_scoped_tier(plan_tier)
    # It deliberately does not substitute a floor, so an unknown, org-scoped or
    # missing key lands here as None — the fail-closed default. A LEGACY key
    # (basic/pro/business) does resolve, to the tier carrying the same
    # capabilities, so ``resolved_key`` reports the current name for a site whose
    # document still holds the old one.
    resolved_key = tier.key if tier is not None else site_plan_catalog.BASE_SITE_PLAN_KEY

    subscription_active = _subscription_is_active(subscription_status)

    # --- FLOOR grants: what the base tier confers with nobody paying -------- #
    # The rule lives in ``site_domain_allowance`` because the attach seam asks it
    # per row too, and one rule written twice is one rule that drifts. A lapsed
    # paid site lands on free's one domained site rather than on zero — losing a
    # subscription must not leave a customer worse off than never having had one.
    max_domained_sites = site_domain_allowance(
        plan_tier=plan_tier, subscription_status=subscription_status
    )

    # --- PAID grants: the tier AND an active subscription ------------------- #
    # Written as an explicit branch rather than ``paid and tier.x`` so the
    # None-narrowing is visible to the type checker instead of resting on
    # short-circuit evaluation.
    badge_removal = False
    concierge_entitled = False
    if tier is not None and subscription_active:
        badge_removal = tier.badge_removal
        # Any tier ABOVE the free floor sells the concierge. The rule itself now
        # lives on the catalog row (``SitePlanTier.sells_concierge``) because the
        # plan-catalog DTO needs the same answer for the buyer-facing plan cards;
        # read it, do not re-express it. What stays HERE is the AND with an active
        # subscription, which is this resolver's whole job.
        concierge_entitled = tier.sells_concierge

    return SiteEntitlements(
        site_id=site_id,
        workspace_id=workspace_id,
        plan_tier=resolved_key,
        subscription_active=subscription_active,
        badge_required=not badge_removal,
        # "May this site have a custom domain at all" — derived, never stored
        # twice. Read off the allowance rather than ``cloudflare_features``, which
        # goes back to meaning only RESOLD Cloudflare capability (BC-10). Whether
        # the WORKSPACE has room for one more is a COUNT, and counting needs the
        # site collection this module may not import (EE cloud rule 2), so it lives
        # at the attach seam in ``sites.service``.
        custom_domain=max_domained_sites != 0,
        max_domained_sites=max_domained_sites,
        # Echoed unchanged — the owner's switch is not a billing question. The
        # AND of the two is ``concierge_available``, which is what seams ask.
        concierge_enabled=bool(concierge_enabled),
        concierge_entitled=concierge_entitled,
    )
