# ee/pocketpaw_ee/cloud/billing/site_plans.py — the SITE-PLAN CATALOG: the
# declarative view of the PER-SITE annual plan tiers (BC-9, the per-site plan
# layer). The Webflow model — each PUBLISHED site carries its OWN recurring
# ANNUAL plan on a tier, distinct from the workspace plan (``billing.plans``).
#
# This is the read-only catalog the per-site subscription flow (publish_pocket)
# builds on. For each site tier it pairs:
#   * ``annual_price_usd`` — the recurring annual price for the tier (USD,
#     whole dollars). The intended sticker; the live charge runs through Dodo.
#   * ``dodo_product_id`` — the Dodo recurring-product id for the tier, or None
#     until config populates it (read from the ``POCKETPAW_DODO_SITE_PRODUCTS``
#     mapping setting when configured). None v1 degrades gracefully — the publish
#     records the intended tier WITHOUT a live charge.
#   * ``cloudflare_features`` — the set of Cloudflare features a higher tier
#     resells (BC-10 provisions these on publish). The base tier resells none.
#   * ``badge_removal`` — whether a site on this tier may ship WITHOUT the
#     free-tier attribution badge. This is the per-site paid tier's headline
#     feature, so it lives on the tier rather than on the workspace plan.
#
# Read-only by design (mirrors ``billing.plans``): no DB, no writes, no emit.
# ``list_site_plans`` / ``get_site_plan`` return frozen ``SitePlanTier`` value
# objects built fresh from the catalog constants + config, so the catalog can
# never drift.
#
# Created 2026-06-24 (integration/billing-credits, BC-9): new module.
# Updated 2026-08-19 (feat/site-plan-catalog-inclusions): added the
#   ``sells_concierge`` property — the catalog-level "does this tier sell the
#   concierge", lifted out of ``resolve_site_entitlements`` where it lived as an
#   inline ``tier.key != BASE_SITE_PLAN_KEY``. Two callers now need the same
#   answer (the resolver, and the plan-catalog DTO the buyer-facing plan cards
#   read), and one rule expressed twice is one rule that drifts.
# Updated 2026-08-13 (feat/sites-free-badge): added ``badge_removal`` — the gate
#   ``sites.badge`` reads to decide whether a publish must stamp the attribution
#   badge. The base tier does NOT carry it (that is what free means); the paid
#   tiers do. Sourced from its own constant rather than folded into
#   ``cloudflare_features``, which is specifically about RESOLD Cloudflare
#   capability and would be the wrong home for a billing-policy flag.
#   NOTE for the pricing-spec migration (step 3 of the build order in
#   docs/design/drafts/2026-08-13-paw-sites-pricing-spec.md): when basic/pro/
#   business are rekeyed to free/site/staff, this mapping moves with them and is
#   the ONLY place the badge's plan gate is expressed.

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# The site-plan ladder. Each tier names its annual price + the Cloudflare
# features it resells. Round, modest defaults — a real price sheet tunes them,
# but the SHAPE (a growing ladder, base resells nothing) is the contract.
#
#   basic    — $0/yr    — the included tier; no resold Cloudflare features.
#   pro      — $120/yr  — adds a custom domain + analytics.
#   business — $480/yr  — adds the WAF + edge cache controls on top of pro.
# ---------------------------------------------------------------------------
_SITE_PLAN_ANNUAL_PRICE_USD: dict[str, int] = {
    "basic": 0,
    "pro": 120,
    "business": 480,
}

# The Cloudflare features each tier resells (BC-10 provisions them on publish).
# A higher tier is a superset of the one below it.
_SITE_PLAN_CF_FEATURES: dict[str, frozenset[str]] = {
    "basic": frozenset(),
    "pro": frozenset({"custom_domain", "analytics"}),
    "business": frozenset({"custom_domain", "analytics", "waf", "edge_cache"}),
}

# Whether a tier may ship a site WITHOUT the attribution badge. ``basic`` is the
# free floor and keeps its badge — that is the whole difference between free and
# paid. Absent/unknown keys resolve False in ``_build``, so a typo means BADGED:
# fail-closed, matching ``sites.badge``'s posture everywhere else.
_SITE_PLAN_BADGE_REMOVAL: dict[str, bool] = {
    "basic": False,
    "pro": True,
    "business": True,
}

# Order the catalog is listed in — the price ladder, cheapest first.
_SITE_TIER_ORDER: tuple[str, ...] = ("basic", "pro", "business")

# The base/floor site tier — a publish with no explicit tier resolves here.
BASE_SITE_PLAN_KEY = "basic"


@dataclass(frozen=True)
class SitePlanTier:
    """One row of the per-site plan catalog — the declarative view of a site tier.

    ``key`` matches the ``Site.plan_tier`` string. ``annual_price_usd`` is the
    recurring annual sticker (USD, whole dollars). ``dodo_product_id`` is the
    recurring-product id, or None until config populates it. ``cloudflare_features``
    is the set of Cloudflare features the tier resells (BC-10 provisions them).
    ``badge_removal`` is whether a site on this tier may ship without the
    attribution badge — read by ``sites.badge.badge_required``. ``sells_concierge``
    is derived, not stored (see the property).
    """

    key: str
    annual_price_usd: int
    dodo_product_id: str | None
    cloudflare_features: frozenset[str]
    badge_removal: bool = False

    @property
    def sells_concierge(self) -> bool:
        """Does this tier sell the visitor concierge at all?

        Derived from the floor rather than a per-tier catalog dict like
        ``badge_removal``: no tier grants concierge today and the tier that will
        (``staff``) does not exist until the pricing-spec rekey, which is blocked
        on an open decision. A dict would need a basic/pro/business mapping
        invented now and rewritten then; "any tier above the floor" needs no
        mapping and survives the rekey untouched.

        This is the CATALOG question — "does this tier sell it" — and on its own
        entitles nobody. ``resolve_site_entitlements`` ANDs it with an active
        subscription to answer "may THIS site serve one", which is the question
        every public seam asks.
        """
        return self.key != BASE_SITE_PLAN_KEY


def _dodo_product_for(key: str) -> str | None:
    """Resolve the Dodo recurring-product id for a site tier, or None.

    Reads an optional ``POCKETPAW_DODO_SITE_PRODUCTS`` mapping
    (``{tier_key: product_id}``) off settings when present. None is the correct
    default in v1 — the per-site sub degrades to "record the tier, skip the live
    charge" when no product is configured. Lazy ``get_settings`` import so building
    the catalog never forces config load in a context that doesn't have it (e.g. a
    unit test of ``list_site_plans``); any config error degrades safely to None
    rather than breaking the read. Mirrors ``billing.plans._dodo_product_for``.
    """
    try:
        from pocketpaw.config import get_settings

        mapping = getattr(get_settings(), "dodo_site_products", None)
    except Exception:
        return None
    if not isinstance(mapping, dict):
        return None
    val = mapping.get(key)
    return val if isinstance(val, str) and val else None


def _build(key: str) -> SitePlanTier:
    """Construct a ``SitePlanTier`` for ``key`` from the catalog constants + config.

    An unknown ``key`` yields a 0-price, no-feature tier — but callers go through
    ``get_site_plan`` / ``list_site_plans``, which only ever pass known keys.
    """
    return SitePlanTier(
        key=key,
        annual_price_usd=_SITE_PLAN_ANNUAL_PRICE_USD.get(key, 0),
        dodo_product_id=_dodo_product_for(key),
        cloudflare_features=_SITE_PLAN_CF_FEATURES.get(key, frozenset()),
        badge_removal=_SITE_PLAN_BADGE_REMOVAL.get(key, False),
    )


def list_site_plans() -> list[SitePlanTier]:
    """Return the full site-plan catalog, cheapest tier first.

    Each ``SitePlanTier`` is built fresh from the catalog constants + config, so
    the catalog always reflects the current configuration.
    """
    return [_build(key) for key in _SITE_TIER_ORDER]


def get_site_plan(key: str | None) -> SitePlanTier | None:
    """Resolve a single site tier by its ``key`` (the ``Site.plan_tier`` string).

    Returns None for an unknown / missing key. Callers that need a guaranteed
    floor map a None back to ``BASE_SITE_PLAN_KEY`` themselves — this function does
    NOT silently substitute, so a typo in a lookup is visible rather than masked
    (mirrors ``billing.plans.get_plan``).
    """
    if not key or key not in _SITE_PLAN_ANNUAL_PRICE_USD:
        return None
    return _build(key)


__all__ = [
    "BASE_SITE_PLAN_KEY",
    "SitePlanTier",
    "get_site_plan",
    "list_site_plans",
]
