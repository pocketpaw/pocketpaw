# ee/pocketpaw_ee/cloud/billing/site_plans.py — the SITE-PLAN CATALOG: the
# declarative view of the PER-SITE annual plan tiers (BC-9, the per-site plan
# layer). The Webflow model — each PUBLISHED site carries its OWN recurring
# ANNUAL plan on a tier, distinct from the workspace plan (``billing.plans``).
#
# This is the read-only catalog the per-site subscription flow (publish_pocket)
# builds on. For each site tier it pairs:
#   * ``monthly_price_usd`` — the recurring MONTHLY price for the tier (USD,
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
# Updated 2026-08-21 (feat/site-free-custom-domain, PW-1): added
#   ``max_domained_sites`` — HOW MANY SITES in a workspace may carry a custom
#   domain on this tier (None = uncapped). The floor now carries 1 rather than 0,
#   which is the captain's rule of 2026-08-21: "only 1 site is allowed to have a
#   custom domain in free". THE UNIT IS THE SITE, NOT THE HOSTNAME — apex and
#   ``www`` on one site cost one, not two — which is why the field is not called
#   ``max_custom_domains``. Custom-domain entitlement now reads this field rather
#   than ``"custom_domain" in cloudflare_features``, so ``cloudflare_features``
#   goes back to meaning only what its name says: RESOLD Cloudflare capability
#   that BC-10 provisions. Also added ``_FREE_MAX_HOSTNAMES_PER_SITE`` — see its
#   own comment for why a site-unit cap needs a hostname-unit companion.

# Updated 2026-08-21 (feat/site-plan-purchasable): added the ``purchasable``
# property — "can a customer actually buy this tier right now". It is not a new
# rule, it is an existing one that had no name: a paid tier with no configured
# Dodo product cannot open a checkout, so ``publish_pocket`` skips charge-first
# and publishes live with no charge. Nothing on the wire said so, which meant the
# storefront happily offered an upgrade button that took no money and granted no
# capability. Naming it lets the card say so instead.
#
# It was unbuyable everywhere until today for a duller reason than "unconfigured":
# ``_dodo_product_for`` reads ``dodo_site_products`` off settings, and that field
# was never declared, so the read always found None no matter what the environment
# said. The field exists now.

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# The site-plan ladder. Each tier names its annual price + the Cloudflare
# features it resells. Round, modest defaults — a real price sheet tunes them,
# but the SHAPE (a growing ladder, base resells nothing) is the contract.
#
#   basic    — $0/yr    — the included tier; no resold Cloudflare features.
#   pro      — $120/yr  — adds analytics + uncapped custom domains.
#   business — $480/yr  — adds the WAF + edge cache controls on top of pro.
# ---------------------------------------------------------------------------
# MONTHLY, and the rename is load-bearing rather than cosmetic: a field called
# ``annual_price_usd`` holding 5 reads as $5/YEAR to every future caller, which is
# below the Cloudflare floor for a single custom hostname ($0.10/hostname/month
# past the first 100). The old $120/$480 annual figures had no cost basis at all.
#
# Decided 2026-08-22. See docs/design/drafts/2026-08-21-paw-sites-pricing-revision.md
# for the floor, the market comparables, and why annual-only billing was its own
# conversion problem.
_SITE_PLAN_MONTHLY_PRICE_USD: dict[str, int] = {
    "basic": 0,
    "pro": 5,
    "business": 19,
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
# The concierge is the ENTIRE difference between the $5 and $19 rungs, so it has
# to be mapped per tier rather than derived. It used to be "any tier above the
# floor", which was correct only while the tier meant to sell it did not exist;
# once the ladder has two paid rungs that derivation hands the cheap one the
# expensive one's feature.
#
# ``.get(key, False)`` fails CLOSED: an unknown tier sells no concierge.
_SITE_PLAN_SELLS_CONCIERGE: dict[str, bool] = {
    "basic": False,
    "pro": False,
    "business": True,
}

_SITE_PLAN_BADGE_REMOVAL: dict[str, bool] = {
    "basic": False,
    "pro": True,
    "business": True,
}

# How many SITES in a workspace may carry a custom domain on this tier.
# ``None`` means uncapped.
#
# THE UNIT IS THE SITE, NOT THE HOSTNAME. A site pointing both ``acme.com`` and
# ``www.acme.com`` at itself spends ONE of these, not two — which is the pair
# almost every customer wants and the reason this is not named
# ``max_custom_domains``. A reader who takes the name literally counts
# ``SiteDomain`` rows, and ``SiteDomain`` is one row per hostname.
#
# The floor carries 1, not 0: "only 1 site is allowed to have a custom domain in
# free" (captain, 2026-08-21). That 1 is a FLOOR GRANT — it needs no subscription,
# unlike every other capability on this catalog — so the resolver reads it off the
# base tier whether or not the site is paying. Unknown keys resolve to 0 in
# ``_build``: fail-closed, matching ``badge_removal``.
_SITE_PLAN_MAX_DOMAINED_SITES: dict[str, int | None] = {
    "basic": 1,
    "pro": None,
    "business": None,
}

# How many HOSTNAMES one FLOOR-tier site may carry. The companion cap to
# ``max_domained_sites``, and it exists because that field caps sites: without
# this, a free workspace can point fifty hostnames at its one allowed site, each
# one costing a Cloudflare custom hostname and a Worker route at $0 revenue. Two
# is apex + ``www``.
#
# Deliberately a single named constant with a single comparison — this is a
# recommendation the build made, not a rule the captain handed down, so raising it
# or deleting it is a one-line change and nothing else moves. Paid tiers are not
# subject to it.
_FREE_MAX_HOSTNAMES_PER_SITE = 2

# Order the catalog is listed in — the price ladder, cheapest first.
_SITE_TIER_ORDER: tuple[str, ...] = ("basic", "pro", "business")

# The base/floor site tier — a publish with no explicit tier resolves here.
BASE_SITE_PLAN_KEY = "basic"


@dataclass(frozen=True)
class SitePlanTier:
    """One row of the per-site plan catalog — the declarative view of a site tier.

    ``key`` matches the ``Site.plan_tier`` string. ``monthly_price_usd`` is the
    recurring annual sticker (USD, whole dollars). ``dodo_product_id`` is the
    recurring-product id, or None until config populates it. ``cloudflare_features``
    is the set of Cloudflare features the tier resells (BC-10 provisions them).
    ``badge_removal`` is whether a site on this tier may ship without the
    attribution badge — read by ``sites.badge.badge_required``. ``sells_concierge``
    and ``purchasable`` are derived, not stored (see the properties).
    ``max_domained_sites`` is how many SITES in the workspace may carry a custom
    domain on this tier (None = uncapped) — the site, not the hostname, so apex +
    ``www`` on one site spend one.
    """

    key: str
    monthly_price_usd: int
    dodo_product_id: str | None
    cloudflare_features: frozenset[str]
    badge_removal: bool = False
    max_domained_sites: int | None = 0

    @property
    def purchasable(self) -> bool:
        """Can a customer actually buy this tier right now?

        A $0 tier is always purchasable — there is nothing to buy, so selecting it
        always succeeds. A priced tier needs a configured Dodo recurring product,
        because without one ``publish_pocket`` cannot open a checkout and falls
        back to publishing live and recording the tier with no charge. The site
        then holds a paid ``plan_tier`` with ``subscription_status="none"``, which
        every entitlement resolves against as the free floor.

        Derived rather than stored, so it tracks configuration rather than a
        deploy-time snapshot. Surfaced on the plan-catalog DTO so a buyer-facing
        card can mark a tier unavailable instead of offering a button that quietly
        does nothing.
        """
        return self.monthly_price_usd == 0 or self.dodo_product_id is not None

    @property
    def sells_concierge(self) -> bool:
        """Does this tier sell the visitor concierge at all?

        Mapped per tier as of 2026-08-22, having previously been derived as "any
        tier above the floor". That derivation was right only while no tier
        actually sold the concierge — under the $0/$5/$19 ladder the concierge IS
        the difference between the two paid rungs, so deriving it would give the
        $5 tier the thing the $19 tier is for.

        This is the CATALOG question — "does this tier sell it" — and on its own
        entitles nobody. ``resolve_site_entitlements`` ANDs it with an active
        subscription to answer "may THIS site serve one", which is the question
        every public seam asks.
        """
        return _SITE_PLAN_SELLS_CONCIERGE.get(self.key, False)


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
        monthly_price_usd=_SITE_PLAN_MONTHLY_PRICE_USD.get(key, 0),
        dodo_product_id=_dodo_product_for(key),
        cloudflare_features=_SITE_PLAN_CF_FEATURES.get(key, frozenset()),
        badge_removal=_SITE_PLAN_BADGE_REMOVAL.get(key, False),
        # ``.get(key, 0)`` and not ``.get(key)``: a missing key must mean NO
        # domains, while a present key mapped to None means UNCAPPED. Collapsing
        # the two would hand an unknown tier the uncapped answer.
        max_domained_sites=_SITE_PLAN_MAX_DOMAINED_SITES.get(key, 0),
    )


def free_max_hostnames_per_site() -> int:
    """How many hostnames one FLOOR-tier site may carry.

    A function rather than a bare constant import so the one seam that enforces it
    (``sites.service.add_domain``) reads it through the catalog's public surface,
    the same way it reads every other plan rule. See the constant's comment for why
    a site-unit cap needs a hostname-unit companion at all.
    """
    return _FREE_MAX_HOSTNAMES_PER_SITE


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
    if not key or key not in _SITE_PLAN_MONTHLY_PRICE_USD:
        return None
    return _build(key)


__all__ = [
    "BASE_SITE_PLAN_KEY",
    "SitePlanTier",
    "free_max_hostnames_per_site",
    "get_site_plan",
    "list_site_plans",
]
