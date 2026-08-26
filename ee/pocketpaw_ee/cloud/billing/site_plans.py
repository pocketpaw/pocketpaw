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
#
# Updated 2026-08-22 (feat/site-pricing-ladder): the catalog now IS the pricing
# spec — five tiers on the captain's approved ladder (free / site / staff /
# studio / agency), rekeyed off the placeholder basic/pro/business names, at
# $0 / $7 / $19 / $39 / $149 a month.
#
# THE REKEY IS THE RISKY HALF, and it is handled by aliasing rather than by a
# flag day. ``Site.plan_tier`` holds the OLD strings in production, and an
# unrecognised key resolves to None — which drops the site to the free floor,
# returning its badge and revoking its custom domain. So ``basic``/``pro``/
# ``business`` remain resolvable FOREVER through ``_LEGACY_SITE_TIER_ALIASES``,
# and resolve to the tier that carries the same capabilities they always did
# (pro sold badge-removal and domains = ``site``; business added the concierge =
# ``staff``). ``scripts/migrate_site_plan_keys.py`` rewrites the stored values so
# the aliases go quiet; nothing breaks if it is never run.
#
# THE SAME TRAP EXISTS IN CONFIG and is easier to miss: the deployed
# ``POCKETPAW_DODO_SITE_PRODUCTS`` map is keyed ``{"pro": ..., "business": ...}``.
# A rekey that only renamed the catalog would have made every paid tier
# ``purchasable = False`` the moment it deployed — no checkout, no charge, and a
# publish silently recording the free floor. ``_dodo_product_for`` therefore
# reads the canonical key FIRST and falls back to any legacy alias of it, so the
# existing environment keeps working and the new key wins once it is set.
#
# TWO SCOPES NOW LIVE IN ONE CATALOG, which is new and is the other thing to read
# carefully. ``free``/``site``/``staff`` are PER-SITE: they are bought one site at
# a time and their key lands in ``Site.plan_tier``. ``studio``/``agency`` are
# PER-ORG flats — one subscription covering many sites — and their key must NEVER
# reach ``Site.plan_tier``, because a per-site publish cannot buy an org plan.
# ``scope`` names the difference and ``site_scoped_tier`` enforces it: every
# entitlement seam resolves through that function, so an org key stored on a site
# (by a bug, a hand-edit, or a replayed webhook) fails closed to the floor instead
# of handing one site the whole org's white-label allowance.
#
# The org tiers are CATALOG-ONLY today. There is no org subscription entity, no
# org checkout and no webhook that could activate one, so they carry no Dodo
# product and ``purchasable`` is False for both — the storefront renders them as
# "talk to us", the same shape ``billing.plans`` already uses for Enterprise. That
# is deliberate: a buy button that takes no money and grants nothing is worse than
# an honest contact link. ``white_label`` / ``included_sites`` / the conversation
# meter fields below are CATALOG CLAIMS — what a tier will sell — and no seam
# reads them as an entitlement yet. ``SiteEntitlements`` still does not carry
# them, for the reason its own docstring gives.
#
# Updated 2026-08-26 (feat/site-plans-as-addons): added ``dodo_addon_id`` and its
# resolver ``_dodo_addon_for`` (reading a new ``POCKETPAW_DODO_SITE_ADDONS``
# map), because a paid site now bills as an ADD-ON LINE on the workspace
# subscription instead of opening a subscription of its own.
#
# THE TWO IDS ARE NOT INTERCHANGEABLE and that is why this is a second field
# rather than a reinterpretation of the first. A Dodo add-on is its own entity
# with its own id; ``subscriptions.change_plan`` takes ``addons=[{addon_id,
# quantity}]`` and a product id is rejected there. So the product map stays, the
# add-on map is new, and each is read by the rail it belongs to.
#
# ``purchasable`` now passes on EITHER id. That is deliberate rollout slack: a
# deployment that has the product map set and the add-on map not yet would
# otherwise turn every paid tier unbuyable the moment this shipped, and publish
# paid selections as the free floor — the exact failure the 2026-08-22 rekey note
# above describes. Per-site subscriptions already sold stay live and keep
# renewing through the product half; only NEW purchases take the add-on rail.

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# The site-plan ladder — the captain's approved pricing spec, monthly, in whole
# US dollars.
#
#   free    — $0    — per account. Unlimited builds, badge ON, our subdomain,
#                     and ONE custom-domained site (the floor grant).
#   site    — $7    — per SITE. Custom domain + the badge comes off.
#   staff   — $19   — per SITE. Everything in site, plus the visitor concierge
#                     with 200 conversations a month included.
#   studio  — $39   — per ORG, flat. White-label across 5 included sites.
#   agency  — $149  — per ORG, flat. 25 included sites, SSO + SLA, pooled
#                     conversation credits at the agency rate.
# ---------------------------------------------------------------------------
_SITE_PLAN_MONTHLY_PRICE_USD: dict[str, int] = {
    "free": 0,
    "site": 7,
    "staff": 19,
    "studio": 39,
    "agency": 149,
}

# What a subscription on this tier BUYS: one site, or the whole workspace.
#
# The distinction is load-bearing rather than descriptive. A ``site``-scoped key
# is a legal ``Site.plan_tier``; an ``org``-scoped key is not, and storing one
# there would hand a single site an allowance the org paid for once. Unknown keys
# resolve to ``org`` in ``_build`` — the scope that is NOT a valid per-site tier —
# so a typo fails closed out of the per-site path rather than into it.
SITE_SCOPE = "site"
ORG_SCOPE = "org"
_SITE_PLAN_SCOPE: dict[str, str] = {
    "free": SITE_SCOPE,
    "site": SITE_SCOPE,
    "staff": SITE_SCOPE,
    "studio": ORG_SCOPE,
    "agency": ORG_SCOPE,
}

# The Cloudflare features each tier resells (BC-10 provisions them on publish).
# A higher tier is a superset of the one below it.
_SITE_PLAN_CF_FEATURES: dict[str, frozenset[str]] = {
    "free": frozenset(),
    "site": frozenset({"custom_domain", "analytics"}),
    "staff": frozenset({"custom_domain", "analytics", "waf", "edge_cache"}),
    "studio": frozenset({"custom_domain", "analytics", "waf", "edge_cache"}),
    "agency": frozenset({"custom_domain", "analytics", "waf", "edge_cache"}),
}

# Does this tier sell the visitor concierge at all?
#
# A per-tier map and NOT "any tier above the floor", which is what it used to be.
# That derivation was only ever correct while no tier sold the concierge: under
# this ladder the concierge is precisely the difference between ``site`` and
# ``staff``, so deriving it would hand the $7 rung the $19 rung's feature.
# ``studio`` is white-label hosting, not staffing — it sells the badge removal
# five times over, not the concierge. ``agency`` sells it, which is what its
# pooled conversation rate is for. Unknown keys resolve False in ``_build``.
_SITE_PLAN_SELLS_CONCIERGE: dict[str, bool] = {
    "free": False,
    "site": False,
    "staff": True,
    "studio": False,
    "agency": True,
}

# Whether a tier may ship a site WITHOUT the attribution badge. ``free`` is the
# floor and keeps its badge — that is the whole difference between free and paid.
# Absent/unknown keys resolve False in ``_build``, so a typo means BADGED:
# fail-closed, matching ``sites.badge``'s posture everywhere else.
_SITE_PLAN_BADGE_REMOVAL: dict[str, bool] = {
    "free": False,
    "site": True,
    "staff": True,
    "studio": True,
    "agency": True,
}

# WHITE-LABEL: no Paw marks anywhere across the org, not just the badge off one
# site. A CATALOG CLAIM — the org tiers that carry it cannot be bought yet, and no
# seam reads this field as an entitlement. It exists so the plan card can name what
# separates a $39 org flat from five $7 sites. When org billing lands, this is the
# field the resolver will AND with an active org subscription.
_SITE_PLAN_WHITE_LABEL: dict[str, bool] = {
    "free": False,
    "site": False,
    "staff": False,
    "studio": True,
    "agency": True,
}

# How many sites an ORG-scoped flat includes before the bulk per-site rate starts.
# ``None`` on every per-site tier — the question does not apply to a subscription
# that buys exactly one site. Catalog claim, same caveat as ``white_label``.
_SITE_PLAN_INCLUDED_SITES: dict[str, int | None] = {
    "free": None,
    "site": None,
    "staff": None,
    "studio": 5,
    "agency": 25,
}

# Concierge conversations included per month, and the rate per conversation
# beyond them, in CENTS.
#
# Cents rather than a float: $0.05 has no exact binary representation, and a
# per-conversation rate is multiplied by a count and then billed. The one place
# that must render dollars divides at the edge.
#
# Both are CATALOG CLAIMS. Nothing meters a conversation yet — there is no
# conversation counter, no debit path and no overage — so these describe the
# ladder rather than gate anything. ``SiteEntitlements`` deliberately does NOT
# carry them (see its docstring: a field that always returns 0 reads as
# implemented). The unit is defined in the pricing spec: one visitor thread that
# received at least one agent reply, closing after 24h of inactivity.
_SITE_PLAN_CONVERSATION_ALLOWANCE: dict[str, int] = {
    "free": 0,
    "site": 0,
    "staff": 200,
    "studio": 0,
    "agency": 0,
}

# The list rate is 10 cents; ``agency``'s pooled rate is half of it and is the
# reason the tier exists at its price. Tiers that sell no concierge still carry
# the list rate rather than 0 — a 0 here would read as "free conversations" to
# anything that renders it, which is the opposite of the truth (they get none).
_LIST_CONVERSATION_RATE_CENTS = 10
_SITE_PLAN_CONVERSATION_RATE_CENTS: dict[str, int] = {
    "free": _LIST_CONVERSATION_RATE_CENTS,
    "site": _LIST_CONVERSATION_RATE_CENTS,
    "staff": _LIST_CONVERSATION_RATE_CENTS,
    "studio": _LIST_CONVERSATION_RATE_CENTS,
    "agency": 5,
}

# Buyer-facing name + the one line a plan card leads with. The catalog owns these
# rather than the frontend, for the reason the storefront's own comment gives: a
# blurb keyed on a tier name in the client says nothing the day the keys change,
# and the keys just changed. Mirrors ``billing.plans._PLAN_DISPLAY``.
_SITE_PLAN_DISPLAY: dict[str, tuple[str, str]] = {
    "free": ("Free", "Build and publish as many sites as you like, on a pawsites subdomain."),
    "site": ("Site", "Point your own domain at it and the Paw badge comes off."),
    "staff": ("Staff", "Adds the visitor concierge — 200 conversations a month, then metered."),
    "studio": ("Studio", "White-label across five sites, on one flat bill for the whole org."),
    "agency": ("Agency", "Twenty-five sites with SSO, an SLA, and pooled conversation credits."),
}

# Extra selling points that are NOT capability flags, and the distinction matters
# enough to keep them in their own field. Every other entry in this catalog is
# something code can check; these are commitments a human honours. Putting SSO in
# ``_SITE_PLAN_CF_FEATURES`` would make it look enforced by something.
_SITE_PLAN_HIGHLIGHTS: dict[str, tuple[str, ...]] = {
    "free": (),
    "site": (),
    "staff": (),
    "studio": ("No Paw marks anywhere", "One bill for the whole org"),
    "agency": ("Single sign-on", "Service-level agreement", "Pooled conversation credits"),
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
# free" (captain, 2026-08-21, reaffirmed against the pricing spec 2026-08-22 —
# the spec itself says subdomain-only, and this is the one place we knowingly
# depart from it). That 1 is a FLOOR GRANT — it needs no subscription, unlike
# every other capability on this catalog — so the resolver reads it off the base
# tier whether or not the site is paying. Unknown keys resolve to 0 in ``_build``:
# fail-closed, matching ``badge_removal``.
#
# The ORG tiers are ``None`` here and carry their real allowance in
# ``included_sites`` instead. This field answers "how many domained sites does
# THIS SITE's own plan allow", and an org key can never be a site's own plan —
# ``site_scoped_tier`` refuses it before this field is ever read.
_SITE_PLAN_MAX_DOMAINED_SITES: dict[str, int | None] = {
    "free": 1,
    "site": None,
    "staff": None,
    "studio": None,
    "agency": None,
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

# Order the catalog is listed in — the price ladder, cheapest first, per-site
# tiers before the org flats.
_SITE_TIER_ORDER: tuple[str, ...] = ("free", "site", "staff", "studio", "agency")

# The base/floor site tier — a publish with no explicit tier resolves here.
BASE_SITE_PLAN_KEY = "free"

# The keys this catalog shipped under before 2026-08-22, mapped to the tier that
# carries the same capabilities today. ``Site.plan_tier`` holds these strings in
# production and NOTHING rewrites a stored document on read, so dropping them
# would silently demote every already-published site to the free floor: badge
# back, custom domain revoked, concierge off.
#
# They are permanent, not transitional. The migration script exists to make them
# unnecessary, not to make them removable — a restored backup, a replayed webhook
# or an old client can still present one years from now, and resolving it costs a
# dict lookup.
#
# The mapping is by CAPABILITY, not by ladder position: ``pro`` sold badge removal
# and uncapped domains with no concierge, which is exactly ``site``; ``business``
# added the concierge, which is exactly ``staff``. A position-based mapping would
# have put ``business`` on ``studio``.
_LEGACY_SITE_TIER_ALIASES: dict[str, str] = {
    "basic": "free",
    "pro": "site",
    "business": "staff",
}


@dataclass(frozen=True)
class SitePlanTier:
    """One row of the per-site plan catalog — the declarative view of a site tier.

    ``key`` matches the ``Site.plan_tier`` string FOR SITE-SCOPED TIERS ONLY; an
    org-scoped row's key is never a legal value there (see ``scope``).
    ``monthly_price_usd`` is the recurring MONTHLY sticker (USD, whole dollars).
    ``dodo_product_id`` is the recurring-product id, or None until config
    populates it. ``cloudflare_features`` is the set of Cloudflare features the
    tier resells (BC-10 provisions them). ``badge_removal`` is whether a site on
    this tier may ship without the attribution badge — read by
    ``sites.badge.badge_required``. ``sells_concierge`` is whether the tier sells
    the visitor concierge at all. ``max_domained_sites`` is how many SITES in the
    workspace may carry a custom domain on this tier (None = uncapped) — the site,
    not the hostname, so apex + ``www`` on one site spend one.

    ``scope`` is ``"site"`` or ``"org"`` and decides which of the two billing
    shapes this row is. Read it before doing anything with ``key``.

    ``white_label``, ``included_sites``, ``conversation_allowance`` and
    ``conversation_rate_cents`` are CATALOG CLAIMS — what the tier will sell. No
    seam gates on them, and ``SiteEntitlements`` deliberately does not carry them.
    They are here so a plan card can describe the ladder honestly; do not mistake
    one for a resolved permission.

    ``sells_concierge``, ``is_org_scoped`` and ``purchasable`` are derived, not
    stored (see the properties).
    """

    key: str
    monthly_price_usd: int
    dodo_product_id: str | None
    cloudflare_features: frozenset[str]
    # Sits here rather than next to ``dodo_product_id``, where it belongs by
    # meaning, because a defaulted dataclass field cannot precede an undefaulted
    # one and ``cloudflare_features`` has no default. It defaults so the existing
    # direct constructions (tests, fixtures) keep working unchanged.
    dodo_addon_id: str | None = None
    scope: str = ORG_SCOPE
    badge_removal: bool = False
    max_domained_sites: int | None = 0
    white_label: bool = False
    included_sites: int | None = None
    conversation_allowance: int = 0
    conversation_rate_cents: int = _LIST_CONVERSATION_RATE_CENTS
    display_name: str = ""
    tagline: str = ""
    highlights: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_org_scoped(self) -> bool:
        """Is this an ORG flat rather than a per-site subscription?

        The one question every caller holding a ``Site.plan_tier`` needs answered
        before it trusts the key. A property rather than ``tier.scope == "org"``
        repeated at each call site, because a string comparison typo'd to
        ``"orgs"`` is silently False — which is the unsafe direction.
        """
        return self.scope == ORG_SCOPE

    @property
    def sells_concierge(self) -> bool:
        """Does this tier sell the visitor concierge at all?

        A PROPERTY over ``_SITE_PLAN_SELLS_CONCIERGE`` rather than a field
        populated in ``_build``, and the difference is not stylistic: the resolver
        test in ``tests/cloud/billing/test_site_plan_inclusions.py`` overrides this
        property to prove ``resolve_site_entitlements`` actually READS the catalog
        rather than re-deriving "any tier above the floor" inline. A dataclass
        field cannot be overridden that way — the instance value shadows the class
        attribute — so making it a field would silently turn that test into one
        that passes against the bug it was written to catch.

        This is the CATALOG question — "does this tier sell it" — and on its own
        entitles nobody. ``resolve_site_entitlements`` ANDs it with an active
        subscription to answer "may THIS site serve one", which is the question
        every public seam asks.
        """
        return _SITE_PLAN_SELLS_CONCIERGE.get(self.key, False)

    @property
    def purchasable(self) -> bool:
        """Can a customer actually buy this tier right now?

        A $0 tier is always purchasable — there is nothing to buy, so selecting it
        always succeeds. A priced tier needs a configured Dodo recurring product,
        because without one ``publish_pocket`` cannot open a checkout and falls
        back to publishing live and recording the tier with no charge. The site
        then holds a paid ``plan_tier`` with ``subscription_status="none"``, which
        every entitlement resolves against as the free floor.

        AN ORG-SCOPED TIER IS NEVER PURCHASABLE HERE, whatever config says. The
        per-site checkout is the only checkout that exists, and it buys one site;
        pointing it at an org flat would charge the org price and grant one site's
        worth of capability. The storefront renders these as "talk to us" — the
        shape ``billing.plans`` already uses for Enterprise — until an org
        subscription entity exists to buy them properly.

        Derived rather than stored, so it tracks configuration rather than a
        deploy-time snapshot.
        """
        if self.is_org_scoped:
            return False
        if self.monthly_price_usd == 0:
            return True
        # EITHER rail makes a tier buyable, and the ADD-ON one is the rail new
        # purchases take. ``dodo_product_id`` is kept in the OR because the
        # per-site subscriptions it opened are live in production: a deployment
        # mid-rollout has the product map set and the add-on map not yet, and
        # returning False there would make every paid tier abruptly unbuyable and
        # publish paid selections as the free floor. Once the add-on map is
        # configured everywhere, the product half is only reached by rows that
        # already hold a per-site subscription.
        return self.dodo_addon_id is not None or self.dodo_product_id is not None


def canonical_site_tier_key(key: str | None) -> str | None:
    """Map any tier key this catalog has ever shipped to its current name.

    Returns the canonical key for a live tier, the aliased key for a legacy one,
    and None for anything else — including None itself, so callers can pass a
    nullable ``Site.plan_tier`` straight in.

    Public because two things outside the catalog need the same answer: the
    migration script (which rewrites stored values) and any log line that wants
    to report what an old key became.
    """
    if not key:
        return None
    if key in _SITE_PLAN_MONTHLY_PRICE_USD:
        return key
    return _LEGACY_SITE_TIER_ALIASES.get(key)


def _dodo_product_for(key: str) -> str | None:
    """Resolve the Dodo recurring-product id for a site tier, or None.

    Reads an optional ``POCKETPAW_DODO_SITE_PRODUCTS`` mapping
    (``{tier_key: product_id}``) off settings when present. None is the correct
    default in v1 — the per-site sub degrades to "record the tier, skip the live
    charge" when no product is configured. Lazy ``get_settings`` import so building
    the catalog never forces config load in a context that doesn't have it (e.g. a
    unit test of ``list_site_plans``); any config error degrades safely to None
    rather than breaking the read. Mirrors ``billing.plans._dodo_product_for``.

    THE CANONICAL KEY WINS, THEN ANY LEGACY ALIAS OF IT. The deployed environment
    is keyed ``{"pro": ..., "business": ...}``, and a rename that only looked up
    the new name would have found nothing — turning every paid tier unpurchasable
    on deploy, with publishes quietly recording the free floor. Looking through
    the alias means the running config keeps working and a re-keyed config takes
    precedence the moment it is set.
    """
    try:
        from pocketpaw.config import get_settings

        mapping = getattr(get_settings(), "dodo_site_products", None)
    except Exception:
        return None
    if not isinstance(mapping, dict):
        return None
    candidates = [key] + [old for old, new in _LEGACY_SITE_TIER_ALIASES.items() if new == key]
    for candidate in candidates:
        val = mapping.get(candidate)
        if isinstance(val, str) and val:
            return val
    return None


def _dodo_addon_for(key: str) -> str | None:
    """Resolve the Dodo ADD-ON id for a site tier, or None.

    The add-on analogue of ``_dodo_product_for``, and a SEPARATE map because a
    Dodo add-on is its own entity with its own id — it is not a product id and
    the two are not interchangeable at the API.

    Reads an optional ``POCKETPAW_DODO_SITE_ADDONS`` mapping
    (``{tier_key: addon_id}``) off settings when present. None is the correct
    default: a paid publish then records the tier without a live charge, exactly
    as it does with no product configured. Lazy ``get_settings`` import and a
    blanket degrade-to-None for the same reason the product resolver has them —
    building the catalog must never force a config load or raise.

    THE CANONICAL KEY WINS, THEN ANY LEGACY ALIAS OF IT, identically to the
    product map. A deployment keyed ``{"pro": ..., "business": ...}`` keeps
    resolving after the 2026-08-22 rename.
    """
    try:
        from pocketpaw.config import get_settings

        mapping = getattr(get_settings(), "dodo_site_addons", None)
    except Exception:
        return None
    if not isinstance(mapping, dict):
        return None
    candidates = [key] + [old for old, new in _LEGACY_SITE_TIER_ALIASES.items() if new == key]
    for candidate in candidates:
        val = mapping.get(candidate)
        if isinstance(val, str) and val:
            return val
    return None


def _build(key: str) -> SitePlanTier:
    """Construct a ``SitePlanTier`` for ``key`` from the catalog constants + config.

    An unknown ``key`` yields a 0-price, no-feature, ORG-scoped tier — org because
    that is the scope which is NOT a legal ``Site.plan_tier``, so an unknown key
    fails closed out of the per-site path. Callers go through ``get_site_plan`` /
    ``list_site_plans``, which only ever pass known keys.
    """
    display_name, tagline = _SITE_PLAN_DISPLAY.get(key, ("", ""))
    return SitePlanTier(
        key=key,
        monthly_price_usd=_SITE_PLAN_MONTHLY_PRICE_USD.get(key, 0),
        dodo_product_id=_dodo_product_for(key),
        dodo_addon_id=_dodo_addon_for(key),
        cloudflare_features=_SITE_PLAN_CF_FEATURES.get(key, frozenset()),
        scope=_SITE_PLAN_SCOPE.get(key, ORG_SCOPE),
        badge_removal=_SITE_PLAN_BADGE_REMOVAL.get(key, False),
        # ``.get(key, 0)`` and not ``.get(key)``: a missing key must mean NO
        # domains, while a present key mapped to None means UNCAPPED. Collapsing
        # the two would hand an unknown tier the uncapped answer.
        max_domained_sites=_SITE_PLAN_MAX_DOMAINED_SITES.get(key, 0),
        white_label=_SITE_PLAN_WHITE_LABEL.get(key, False),
        included_sites=_SITE_PLAN_INCLUDED_SITES.get(key),
        conversation_allowance=_SITE_PLAN_CONVERSATION_ALLOWANCE.get(key, 0),
        conversation_rate_cents=_SITE_PLAN_CONVERSATION_RATE_CENTS.get(
            key, _LIST_CONVERSATION_RATE_CENTS
        ),
        display_name=display_name,
        tagline=tagline,
        highlights=_SITE_PLAN_HIGHLIGHTS.get(key, ()),
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

    ALL FIVE ROWS, both scopes — this is what the storefront renders, and a buyer
    comparing plans needs to see the org flats beside the per-site rungs. A caller
    that means "tiers a site may be published on" wants
    ``list_site_scoped_plans`` instead; picking the wrong one here is how an org
    key reaches ``Site.plan_tier``.

    Each ``SitePlanTier`` is built fresh from the catalog constants + config, so
    the catalog always reflects the current configuration.
    """
    return [_build(key) for key in _SITE_TIER_ORDER]


def list_site_scoped_plans() -> list[SitePlanTier]:
    """The per-site rungs only — the tiers a single site may actually be put on.

    The publish path and the per-site tier picker want this list, never
    ``list_site_plans``: an org flat in a per-site picker offers a purchase that
    cannot happen and a key that must never be stored on the site.
    """
    return [tier for tier in list_site_plans() if not tier.is_org_scoped]


def get_site_plan(key: str | None) -> SitePlanTier | None:
    """Resolve a single tier by its ``key``, accepting any name it has shipped under.

    Returns None for an unknown / missing key. Callers that need a guaranteed
    floor map a None back to ``BASE_SITE_PLAN_KEY`` themselves — this function does
    NOT silently substitute, so a typo in a lookup is visible rather than masked
    (mirrors ``billing.plans.get_plan``).

    Legacy keys resolve to their current tier, so the returned row's ``key`` is the
    CANONICAL one — ``get_site_plan("pro").key == "site"``. That is deliberate: a
    caller that echoes the resolved key back onto a document or into a response
    quietly completes the migration rather than re-persisting the old name.

    RESOLVES BOTH SCOPES. A caller holding a ``Site.plan_tier`` wants
    ``site_scoped_tier`` instead — this one will happily hand back an org flat.
    """
    canonical = canonical_site_tier_key(key)
    if canonical is None:
        return None
    return _build(canonical)


def site_scoped_tier(key: str | None) -> SitePlanTier | None:
    """Resolve ``key`` as A SITE'S OWN plan, or None if it cannot be one.

    The guarded read every entitlement seam uses. It differs from
    ``get_site_plan`` in exactly one way, and that way is the point: an
    ORG-scoped key resolves to None here rather than to a tier.

    ``Site.plan_tier`` is written by the publish path, which only offers per-site
    rungs — so an org key in that field means something went wrong: a bug, a
    hand-edited document, a replayed webhook, a restored backup from a future
    schema. Whatever the cause, the safe reading is "this site has no plan of its
    own", which lands it on the free floor. The unsafe reading is the one a plain
    catalog lookup gives: one site silently holding the white-label allowance an
    org pays $149 a month for.
    """
    tier = get_site_plan(key)
    if tier is None or tier.is_org_scoped:
        return None
    return tier


__all__ = [
    "BASE_SITE_PLAN_KEY",
    "ORG_SCOPE",
    "SITE_SCOPE",
    "SitePlanTier",
    "canonical_site_tier_key",
    "free_max_hostnames_per_site",
    "get_site_plan",
    "list_site_plans",
    "list_site_scoped_plans",
    "site_scoped_tier",
]
