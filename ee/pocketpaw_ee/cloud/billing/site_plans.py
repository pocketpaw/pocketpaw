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
#
# Read-only by design (mirrors ``billing.plans``): no DB, no writes, no emit.
# ``list_site_plans`` / ``get_site_plan`` return frozen ``SitePlanTier`` value
# objects built fresh from the catalog constants + config, so the catalog can
# never drift.
#
# Created 2026-06-24 (integration/billing-credits, BC-9): new module.

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
    """

    key: str
    annual_price_usd: int
    dodo_product_id: str | None
    cloudflare_features: frozenset[str]


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
