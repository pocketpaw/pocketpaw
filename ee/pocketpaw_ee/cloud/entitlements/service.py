# ee/pocketpaw_ee/cloud/entitlements/service.py — the entitlements RESOLVER
# (BC-6, the Entitlement primitive). Module-level ``async def`` API, not a class,
# per EE cloud rule and mirroring ``credits.service`` / ``billing.service``.
#
# WORKSPACE scope:
#   * ``entitlements_from_plan(workspace_id, plan_key)`` — PURE, no DB. The plan
#     catalog's answer for a tier key. The platform console reads it to show an
#     operator the CATALOG value beside the RESOLVED one without a second copy of
#     the tier lookup and its fallback.
#   * ``resolve_entitlements(workspace_id)`` — the above, plus the workspace's
#     current plan, plus any non-expired ``WorkspaceOverrides`` a platform
#     operator has set. THE single choke point every enforcement path in the
#     codebase calls through, which is what makes one override reach all of them.
#
# PER-SITE scope (a different source and cadence — see ``SiteEntitlements``):
#   * ``site_domain_allowance`` / ``site_analytics_entitled`` — pure predicates,
#     shared by the seams that hold two strings rather than a resolved object.
#   * ``resolve_site_entitlements`` — the per-site object, whose fields CALL those
#     predicates rather than re-deriving them.
#
# INVARIANTS a reader must not break:
#   * READ-ONLY. No writes, no ``emit`` (EE cloud rule 9 fires on mutation only).
#   * FAIL CLOSED, everywhere. No plan, an unknown plan, a missing workspace: the
#     ``free`` base tier, never a crash and never a paid-tier leak. Even the
#     defensive branch for "the catalog has lost its base tier" spells out the
#     Free values rather than leaving a ceiling ``None``/uncapped.
#   * PAID GRANTS NEED AN ACTIVE SUBSCRIPTION. Per-site, ``plan_tier`` alone is
#     not evidence of payment — a cancelled subscription leaves the paid key in
#     place, and a paid publish with no Dodo product configured records the tier
#     with ``subscription_status="none"``. Only ``max_domained_sites`` is a FLOOR
#     grant, resolving off the base tier with nobody paying.
#   * TWO SEPARATE DB CALLS, on purpose. ``resolve_entitlements`` calls
#     ``get_workspace_plan`` and ``get_workspace_overrides`` independently, not
#     the combined ``get_workspace_plan_and_overrides``. Tests across the tree
#     drive this resolver by monkeypatching ``get_workspace_plan`` against an id
#     that is not a real Mongo id, and a combined fetch nulls out the mocked plan
#     (44 failures when it was tried). One extra round trip buys every mock.
#   * An EXPIRED override set is wholly absent, not partly applied — an operator
#     must never reason about which fields of one grant outlived the others.
#   * Overrides reach only the fields this resolver enforces.
#     ``monthly_credit_allotment`` and ``features`` are excluded: their
#     enforcement points read the catalog directly and never come through here,
#     so an override on either would store, display, and do nothing (PRD errata
#     C2). See ``WorkspaceOverrides``.

from __future__ import annotations

from typing import TYPE_CHECKING

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing import plans as plan_catalog
from pocketpaw_ee.cloud.billing import site_plans as site_plan_catalog
from pocketpaw_ee.cloud.entitlements.domain import Entitlements, SiteEntitlements

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.models.workspace import WorkspaceOverrides


# Which workspace plans may read the SOURCE CODE of the sites they own. Every
# paid rung does; ``free`` is absent, so a free workspace resolves ``False`` by
# not being named here rather than by being listed as denied.
#
# AN EXPLICIT ALLOW-SET, NOT ``key != BASE_PLAN_KEY``. Deriving a capability by
# negating the floor is how ``SitePlanTier.sells_concierge`` came to claim it
# survived the site-plan rekey when it did not — the middle rung is also "not
# free" and must not sell what the top rung sells. Here the same shortcut would
# grant source to any tier added to the catalog later, silently, on the day it
# was added. A key this set does not name resolves ``False``: a retired one
# (``studio``, ``agency`` — both retired SITE-plan keys that still sit in stored
# documents and resolve to no tier), a typo'd one, or a rung invented next
# quarter. For a capability that exposes code, "unknown means no" is the only
# direction a mistake may fail in.
_SOURCE_VISIBLE_PLANS = frozenset({"go", "pro", "pro_max", "enterprise"})


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
                # And no site source. The catalog having lost its base tier is
                # no reason to hand out code.
                site_source_visible=False,
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
        # Read off the RESOLVED tier's key, never the raw ``plan_key`` argument.
        # An unknown or retired key has already been replaced by the base tier
        # above, so ``free`` is what reaches this line and ``False`` is what it
        # answers. Testing ``plan_key`` here instead would hand a stale document
        # a paid grant.
        site_source_visible=tier.key in _SOURCE_VISIBLE_PLANS,
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


def _resolve_override_flag(catalog_value: bool, override: bool | None) -> bool:
    """Overlay one BOOLEAN capability: ``None`` keeps the catalog answer,
    ``True`` or ``False`` replaces it.

    A separate helper from ``_resolve_override_value``, not a widened one, and
    the reason is ``bool`` being a subclass of ``int``. A flag routed through
    that function would reach its ``isinstance(override, str)`` narrowing as an
    ``int``, so ``False`` would fall to the final ``return override`` and work
    only by accident of the branch order — and the ``"uncapped"`` state it exists
    to express is meaningless for a flag, which already has three states of its
    own (on, off, no opinion).

    Note what ``False`` means here: an operator REVOKING a capability the plan
    grants, which is a real lever (an abuse response) and distinct from ``None``.
    So this cannot be written as ``catalog_value or override`` — that reads a
    revocation as no opinion and leaves the capability on.
    """
    if override is None:
        return catalog_value
    return override


def _apply_overrides(
    entitlements: Entitlements, overrides: WorkspaceOverrides | None
) -> Entitlements:
    """Overlay a workspace's overrides onto its catalog-resolved entitlements.

    An expired override set (``expires_at`` in the past) is treated as
    entirely absent — not partially applied — so an operator is never left
    reasoning about which fields of one grant outlived the others.

    Only the fields ``WorkspaceOverrides`` models are overlaid — seven ceilings
    and one capability flag. ``monthly_credit_allotment`` and ``features`` never
    reach this function at all — see ``WorkspaceOverrides`` for why an override
    on either would be inert (PRD errata C2).
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
        monthly_ceiling=_resolve_override_value(
            entitlements.monthly_ceiling, overrides.monthly_ceiling
        ),
        max_seats=_resolve_override_value(entitlements.max_seats, overrides.max_seats),
        max_pockets=_resolve_override_value(entitlements.max_pockets, overrides.max_pockets),
        max_connectors=_resolve_override_value(
            entitlements.max_connectors, overrides.max_connectors
        ),
        max_call_seconds_per_day=_resolve_override_value(
            entitlements.max_call_seconds_per_day, overrides.max_call_seconds_per_day
        ),
        max_storage_bytes=_resolve_override_value(
            entitlements.max_storage_bytes, overrides.max_storage_bytes
        ),
        included_sites=_resolve_override_value(
            entitlements.included_sites, overrides.included_sites
        ),
        site_source_visible=_resolve_override_flag(
            entitlements.site_source_visible, overrides.site_source_visible
        ),
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
    ceiling, included sites, site-source visibility) with no other code changed.
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


# Which per-site tiers may have the site's PROJECT DOWNLOADED — the built site
# handed back to its owner as an archive rather than only served from our edge.
# The two paid rungs do; ``free`` is absent, so a floor site resolves ``False`` by
# not being named here rather than by being listed as denied.
#
# AN EXPLICIT ALLOW-SET, NOT ``key != BASE_SITE_PLAN_KEY``. Deriving a per-site
# capability by negating the floor is a mistake this catalog has already made
# once: ``sells_concierge`` was written that way, which was only ever correct
# while nothing sold the concierge — the moment ``staff`` did, "not free" handed
# the $7 rung the $19 rung's feature. The same shortcut here would grant the
# download to whatever rung the ladder gains next, silently, on the day it is
# added. A key this set does not name resolves ``False``: a retired one
# (``studio``, ``agency``, both retired on 2026-09-06 and still sitting in stored
# documents), a typo, or a tier invented next quarter.
#
# It holds CANONICAL keys only, and the grant below reads the RESOLVED tier's key
# against it. That is what keeps the legacy aliases working: a site whose document
# still says ``pro`` resolves to ``site`` and is entitled to what it has always
# paid for, without ``pro`` and ``business`` having to be listed here too.
#
# It lives here rather than on ``SitePlanTier`` because one caller needs it. The
# catalog grew ``sells_concierge`` only when a SECOND caller did (the resolver and
# the buyer-facing plan-card DTO), which is the bar for lifting a rule out of the
# resolver; until a plan card sells the download, a catalog field would be a
# second home for a rule with one reader.
_PROJECT_DOWNLOAD_PLANS = frozenset({"site", "staff"})


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
    project_download = False
    if tier is not None and subscription_active:
        badge_removal = tier.badge_removal
        # Any tier ABOVE the free floor sells the concierge. The rule itself now
        # lives on the catalog row (``SitePlanTier.sells_concierge``) because the
        # plan-catalog DTO needs the same answer for the buyer-facing plan cards;
        # read it, do not re-express it. What stays HERE is the AND with an active
        # subscription, which is this resolver's whole job.
        concierge_entitled = tier.sells_concierge
        # ``tier.key`` and not the ``plan_tier`` argument. The resolved key is the
        # CURRENT name for a site whose document still holds a pre-rekey one, so a
        # ``pro`` site is entitled to the download exactly as the ``site`` it
        # resolves to. Matching the raw string instead would quietly demote every
        # site that has not been migrated.
        project_download = tier.key in _PROJECT_DOWNLOAD_PLANS

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
        project_download=project_download,
    )
