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
# Updated 2026-09-02 (feat/sites-analytics-gate, SA-2): added
#   ``site_analytics_entitled`` — "may this site's visitors be counted", the gate
#   the publish path reads before it deploys a pageview counter and the read
#   endpoint will read before it serves the numbers. A module-level PURE function
#   beside ``site_domain_allowance`` rather than a field on ``SiteEntitlements``,
#   for the reason that function's own docstring gives: more than one seam asks it,
#   and one of them (the deploy) holds two strings rather than a resolved
#   entitlements object. Keeping it a function is what makes both seams able to
#   share the single predicate instead of each re-deriving it.
# Updated 2026-09-02 (feat/sites-analytics-entitlement-field, SA-5):
#   ``resolve_site_entitlements`` now also reports that answer as
#   ``SiteEntitlements.analytics``, so the dashboard can disable the analytics panel
#   and say why rather than calling the endpoint to be refused. This does NOT
#   supersede the entry above: the function stays THE predicate and the field CALLS
#   it, exactly as ``max_domained_sites`` calls ``site_domain_allowance``. The
#   function exists for the seams that hold two strings and no resolved object; the
#   field exists for the one reader that already has the object. Neither re-derives
#   the rule, which is the only property that matters.
# Updated 2026-09-16 (Paw Admin chunk 7, Decision 7): ``resolve_entitlements``
#   now also overlays a workspace's ``WorkspaceOverrides`` (set by a platform
#   operator via ``cloud/platform/entitlements.py``) onto the catalog-resolved
#   values, skipping an expired override set entirely. The tier→``Entitlements``
#   construction was extracted to the new ``entitlements_from_plan`` (pure, no
#   DB) so the platform route can show an operator the CATALOG value beside the
#   RESOLVED one without a second copy of the tier-lookup/fallback logic. Only
#   the seven fields ``resolve_entitlements`` itself enforces are overlaid —
#   ``monthly_credit_allotment`` and ``features`` are deliberately excluded; see
#   ``WorkspaceOverrides`` for why (PRD errata C2).
#   CORRECTION, same day: the first cut of this fetched the plan and the
#   overrides off one combined call (``get_workspace_plan_and_overrides``),
#   which broke every test across this codebase (44 failures in
#   ``tests/cloud``) that drives this resolver by monkeypatching
#   ``get_workspace_plan`` — the combined call's own doc lookup failed on
#   those tests' non-Mongo-id workspace ids and nulled out the plan the mock
#   had already answered. Fixed by going back to calling ``get_workspace_plan``
#   directly (so every existing mock keeps working) and adding a second,
#   independent ``get_workspace_overrides`` call for the override half. Costs
#   one extra DB round trip over the original design; correctness for every
#   existing caller outweighs it. ``get_workspace_plan_and_overrides`` is kept
#   for the platform route, which always addresses a real workspace doc by
#   path parameter and has no such mock to preserve.

from __future__ import annotations

from typing import TYPE_CHECKING

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing import plans as plan_catalog
from pocketpaw_ee.cloud.billing import site_plans as site_plan_catalog
from pocketpaw_ee.cloud.entitlements.domain import Entitlements, SiteEntitlements

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.models.workspace import WorkspaceOverrides


def entitlements_from_plan(workspace_id: str, plan_key: str | None) -> Entitlements:
    """Build ``Entitlements`` from a plan key — pure, no DB access.

    Extracted out of ``resolve_entitlements`` so the platform entitlements
    route (Paw Admin chunk 7) can show an operator the plan CATALOG'S values
    beside the OVERRIDE-RESOLVED ones without re-deriving the tier lookup and
    its fallback. ``resolve_entitlements`` is this function plus a DB fetch
    plus the override overlay — nothing about the fallback logic lives twice.

    A workspace with no/unknown plan (``plan_key`` is ``None`` or not in the
    catalog) resolves to the ``free`` base tier — never a crash, never a
    paid-tier leak.
    """
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
                # 0 included sites. Failing closed
                # matters more here than on the ceilings above: those cap spend a
                # workspace is being billed for, where an over-generous default
                # is an overspend somebody can see and correct. This one decides
                # whether a site is billed AT ALL, so a generous default is free
                # hosting that nothing later reclaims.
                included_sites=0,
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
        included_sites=tier.included_sites,
        features=tier.features,
    )


def _resolve_override_value(catalog_value: int | None, override: int | str | None) -> int | None:
    """Overlay one field: ``None`` keeps the catalog value, ``"uncapped"``
    clears it, an int replaces it.

    ``isinstance`` rather than ``override == "uncapped"``: the field's type is
    ``int | Literal["uncapped"] | None``, and a type checker cannot narrow
    ``int | str`` down to ``int`` from an equality comparison — the trailing
    ``return override`` would still read as ``int | str`` against a
    ``-> int | None`` signature. ``isinstance(override, str)`` narrows the
    ``else`` branch to ``int`` correctly, and is equivalent here because the
    only string value the type allows is ``"uncapped"``.
    """
    if override is None:
        return catalog_value
    if isinstance(override, str):
        return None
    return override


def _apply_overrides(
    entitlements: Entitlements, overrides: "WorkspaceOverrides | None"
) -> Entitlements:
    """Overlay a workspace's overrides onto its catalog-resolved entitlements.

    An expired override set (``expires_at`` in the past) is treated as
    entirely absent — not partially applied — so an operator is never left
    reasoning about which fields of one grant outlived the others.

    Only the seven fields ``WorkspaceOverrides`` models are overlaid.
    ``monthly_credit_allotment`` and ``features`` never reach this function at
    all — see ``WorkspaceOverrides`` for why an override on either would be
    inert (PRD errata C2).
    """
    if overrides is None:
        return entitlements

    if overrides.expires_at is not None:
        from datetime import UTC, datetime

        # Mongo (and mongomock) round-trips a naive datetime, so a value read
        # straight back off the document has no tzinfo even though it was
        # written as UTC-aware. Treat naive as UTC rather than let the
        # comparison raise.
        expires_at = overrides.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            return entitlements

    import dataclasses

    return dataclasses.replace(
        entitlements,
        monthly_ceiling=_resolve_override_value(entitlements.monthly_ceiling, overrides.monthly_ceiling),
        max_seats=_resolve_override_value(entitlements.max_seats, overrides.max_seats),
        max_pockets=_resolve_override_value(entitlements.max_pockets, overrides.max_pockets),
        max_connectors=_resolve_override_value(entitlements.max_connectors, overrides.max_connectors),
        max_call_seconds_per_day=_resolve_override_value(
            entitlements.max_call_seconds_per_day, overrides.max_call_seconds_per_day
        ),
        max_storage_bytes=_resolve_override_value(
            entitlements.max_storage_bytes, overrides.max_storage_bytes
        ),
        included_sites=_resolve_override_value(entitlements.included_sites, overrides.included_sites),
    )


async def resolve_entitlements(workspace_id: str) -> Entitlements:
    """Resolve a workspace to its entitlements (plan + features + allotment).

    Reads the workspace's CURRENT ``Workspace.plan`` and looks the tier up in the
    billing plan catalog, then overlays any non-expired ``WorkspaceOverrides`` a
    platform operator has set (Paw Admin chunk 7). A workspace with no/unknown
    plan (or one that doesn't exist) resolves to the ``free`` base tier — never
    a crash, never a paid-tier leak.

    This is the SINGLE choke point every enforcement path in the codebase calls
    through, which is what makes an override here reach all of them (seat caps,
    the LiveKit call-time gate, storage, connectors, pockets, the monthly
    ceiling, included sites) with no other code changed.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("entitlements.invalid_workspace", "workspace_id is required")

    # Lazy import keeps this module free of the heavy workspace.service import at
    # module load (it pulls Beanie), mirroring how the plan-feature gate dep
    # imports the workspace service inside the guard.
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    # Two separate calls, not the combined ``get_workspace_plan_and_overrides``
    # (that one is for the platform route, which always has a real workspace
    # doc behind a path parameter). Every existing consumer of this resolver —
    # tests across billing, credits, connectors, pockets, storage, livekit,
    # chat — drives it by monkeypatching ``get_workspace_plan`` with a plan
    # string against a workspace id that is not a real Mongo id. A combined
    # fetch that resolves both fields off one document lookup would silently
    # discard that patch (the doc lookup fails on the fake id and nulls out a
    # plan the mock already answered), which is exactly the regression this
    # split avoids: ``get_workspace_plan`` stays the one function every mock
    # targets, and only the override lookup touches the DB, failing closed to
    # "no override" for the same fake ids those tests use.
    plan_key = await workspace_service.get_workspace_plan(workspace_id)
    overrides = await workspace_service.get_workspace_overrides(workspace_id)

    base = entitlements_from_plan(workspace_id, plan_key)
    return _apply_overrides(base, overrides)


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


def site_analytics_entitled(*, plan_tier: str | None, subscription_status: str | None) -> bool:
    """May THIS site have its visitors counted?

    THE ONE PREDICATE for visitor analytics, and public for that reason. Two seams
    ask it and they must never disagree: the PUBLISH path (SA-2) decides whether
    the deployed config carries a pageview counter at all, and the read endpoint
    (SA-4) decides whether a site's numbers may be served. A site whose publish
    counted but whose read refuses is a customer paying for a blank chart; the
    reverse serves numbers a site was never entitled to gather.

    Pure and synchronous over the site's two billing fields, exactly like
    ``site_domain_allowance`` beside it, so the caller that owns the ``Site``
    document passes what it owns (EE cloud rule 2 — ``entitlements`` may not import
    ``models.site``).

    A PAID grant, not a floor one, and the difference is money rather than tidiness.
    A Worker invocation is billed and a static asset is not, so counting a free
    site's traffic spends real money against no revenue — the cost has to track the
    plan. The consequence is that upgrading does not backfill: a site's history
    begins at the publish that first carried a counter, because nothing was
    recorded before it. That is a product decision, and it belongs on screen rather
    than in a docstring.

    Tier AND active subscription, the same conjunction every other paid capability
    here uses. Reading the tier alone is the bug this module exists to prevent —
    cancellation leaves ``plan_tier`` on the paid key, and an unconfigured Dodo
    product records a paid tier that was never charged at all.

    Fails closed on every unknown. ``site_scoped_tier`` returns None for a missing
    key, an unrecognised one, and an ORG-scoped key that has no business on a single
    site; all three land on False.
    """
    tier = site_plan_catalog.site_scoped_tier(plan_tier)
    if tier is None or not _subscription_is_active(subscription_status):
        return False
    # Read off the RESOLVED catalog row rather than the raw feature map, so the
    # legacy key aliases (``pro`` → ``site``) resolve here exactly as they do for
    # the badge and the domain allowance. A site whose document still holds a
    # pre-rekey key is entitled to what it has always paid for.
    return site_plan_catalog.ANALYTICS_FEATURE in tier.cloudflare_features


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

    # --- The analytics grant, borrowed rather than re-derived ---------------- #
    # Not folded into the paid branch below as ``ANALYTICS_FEATURE in
    # tier.cloudflare_features``, even though that is what it reduces to.
    # ``site_analytics_entitled`` is already THE predicate for this capability: the
    # publish seam asks it to decide whether the deployed config carries a counter,
    # and the read endpoint asks it to decide whether numbers may be served. A
    # third expression of the rule here would be a third thing to keep in step, and
    # the two ways it can fall out of step are both bad — a site counting traffic it
    # is never shown, or a customer paying for a chart that refuses to load.
    analytics = site_analytics_entitled(
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
        analytics=analytics,
        # Echoed unchanged — the owner's switch is not a billing question. The
        # AND of the two is ``concierge_available``, which is what seams ask.
        concierge_enabled=bool(concierge_enabled),
        concierge_entitled=concierge_entitled,
    )
