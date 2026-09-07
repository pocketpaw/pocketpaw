# ee/pocketpaw_ee/cloud/billing/site_plans.py — the SITE-PLAN CATALOG: the
# declarative view of the PER-SITE annual plan tiers (BC-9, the per-site plan
# layer). The Webflow model — each PUBLISHED site carries its OWN recurring
# ANNUAL plan on a tier, distinct from the workspace plan (``billing.plans``).
#
# This is the read-only catalog the publish flow (publish_pocket) builds on. For
# each site tier it pairs:
#   * ``monthly_price_usd`` — the recurring MONTHLY price for the tier (USD,
#     whole dollars). It is BOTH the sticker and the charge: the publish debits
#     exactly this many dollars, as credits, from the workspace balance.
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
# property — "can a customer actually buy this tier right now". It then meant
# "is a Dodo product configured for it"; see the 2026-09-05 note for what it
# means now that no site plan touches a gateway.
#
# Updated 2026-08-22 (feat/site-pricing-ladder): the catalog now IS the pricing
# spec — five tiers on the captain's approved ladder (free / site / staff /
# studio / agency), rekeyed off the placeholder basic/pro/business names, at
# $0 / $7 / $19 / $39 / $149 a month. (The last two were retired on 2026-09-06;
# the ladder is the three per-site rungs. See the 2026-09-06 note below.)
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
# TWO SCOPES ONCE LIVED IN ONE CATALOG — worth reading because the machinery is
# still here. ``free``/``site``/``staff`` are PER-SITE: they are bought one site at
# a time and their key lands in ``Site.plan_tier``. ``studio``/``agency`` were
# PER-ORG flats — one subscription covering many sites — and their key must NEVER
# reach ``Site.plan_tier``, because a per-site publish cannot buy an org plan.
# Both flats were retired on 2026-09-06, so the catalog ships one scope today; the
# paragraph stays in the present tense because the SHAPE does, and the next
# org-scoped tier inherits it rather than re-deriving it.
# ``scope`` names the difference and ``site_scoped_tier`` enforces it: every
# entitlement seam resolves through that function, so an org key stored on a site
# (by a bug, a hand-edit, or a replayed webhook) fails closed to the floor instead
# of handing one site the whole org's white-label allowance.
#
# Updated 2026-09-06 (feat/plan-included-sites): THE ORG FLATS ARE RETIRED.
#
# ``studio`` ($39, 5 sites) and ``agency`` ($149, 25) are gone, along with the
# ``white_label`` and ``included_sites`` fields that existed only to describe
# them. The WORKSPACE plan carries sites now — Paw Go 1, Pro 3, Pro Max 10, at
# ``staff`` quality — so a second ladder selling sites by the flat fee both
# contradicted it in the storefront and priced worse than Pro Max ($49 for 10) at
# every rung. Neither was purchasable, so nothing was sold on either and no
# customer migration is needed.
#
# ``scope`` / ``is_org_scoped`` / ``site_scoped_tier`` STAY, and are still the
# guard every entitlement seam reads through. Nothing in the catalog is org-scoped
# any more, so the guard now protects against a key that is merely GONE: a Site
# doc that still stores "studio" from before today resolves to None and lands on
# the free floor, exactly as it did when the key resolved to an org flat. The
# unsafe reading — a plain ``get_site_plan`` handing one site an org allowance —
# is unreachable either way, and keeping the seam means re-introducing an org tier
# does not have to re-derive which lookups were safe.

# Updated 2026-09-05 (fix/sites-plan-credits): DODO IS GONE FROM THIS CATALOG.
#
# A per-site plan is paid from the WORKSPACE CREDIT BALANCE — the publish debits
# ``monthly_price_usd * 100`` credits (a credit is a cent) and a monthly sweep
# debits it again each period. So the two gateway id fields (``dodo_product_id``,
# ``dodo_addon_id``), their resolvers and the two settings maps that fed them
# (``POCKETPAW_DODO_SITE_PRODUCTS``, ``POCKETPAW_DODO_SITE_ADDONS``) are all
# removed rather than left unread. A field nothing consumes is a field someone
# reintroduces a dependency on.
#
# ``purchasable`` therefore changed meaning rather than value: it asks "does the
# ladder sell this tier ONE SITE AT A TIME", which is True for every rung the
# catalog now ships — the org flats it answered False for are gone. Whether a
# given workspace can afford a rung today is a question about its balance,
# answered at purchase time with a 402 —
# not a property of the catalog.
#
# The workspace-plan catalog (``billing.plans``) still bills through Dodo and is
# untouched. Only the per-site ladder left the gateway.

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# The site-plan ladder — the captain's approved pricing spec, monthly, in whole
# US dollars.
#
#   free    — $0    — per account. Unlimited builds, watermark ON, our subdomain,
#                     and ONE custom-domained site (the floor grant).
#   site    — $7    — per SITE. Custom domain + the Paw watermark comes off.
#   staff   — $19   — per SITE. Everything in site, plus the visitor concierge
#                     with 200 conversations a month included.
#
# A ``studio`` ($39, 5 sites) and an ``agency`` ($149, 25 sites) sat below those
# until 2026-09-06, both per-ORG flats. They were retired rather than repriced:
# the WORKSPACE plan carries sites now (Go 1 / Pro 3 / Pro Max 10, in
# ``billing.plans._INCLUDED_SITES``), so a second ladder selling a bundle of sites
# for a flat fee both contradicted it and priced worse than Pro Max. Bundled sites
# have exactly one home, and it is the workspace ladder.
#
# What remains here is per-SITE overflow: the rungs a workspace buys for site
# N+1 once its plan's included sites are used up.
# ---------------------------------------------------------------------------
_SITE_PLAN_MONTHLY_PRICE_USD: dict[str, int] = {
    "free": 0,
    "site": 7,
    "staff": 19,
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
}

# The ``cloudflare_features`` member that gates VISITOR ANALYTICS: the pageview
# counter a published site carries (``sites.analytics_worker``, SA-1). Named here
# rather than spelled as a literal at the seams, because more than one seam asks
# the same question — the PUBLISH path decides whether to deploy a counter at all
# (SA-2), and the read endpoint decides whether a site's numbers may be served
# (SA-4). A literal retyped at each of them is one typo away from a free site that
# counts, or a paid one that does not.
#
# It names a member of ``_SITE_PLAN_CF_FEATURES`` below, which is deliberately
# still written as literals — that dict is the declarative catalog and reads best
# flat. ``tests/ee/sites/test_sites_analytics_gate.py`` pins the pairing, so a
# rename of one without the other fails there rather than silently entitling
# nobody: every other test in that file goes through this constant and would keep
# passing against a catalog that no longer mentions the feature at all.
ANALYTICS_FEATURE = "analytics"

# The Cloudflare features each tier resells (BC-10 provisions them on publish).
# A higher tier is a superset of the one below it.
_SITE_PLAN_CF_FEATURES: dict[str, frozenset[str]] = {
    "free": frozenset(),
    "site": frozenset({"custom_domain", "analytics"}),
    "staff": frozenset({"custom_domain", "analytics", "waf", "edge_cache"}),
}

# Does this tier sell the visitor concierge at all?
#
# A per-tier map and NOT "any tier above the floor", which is what it used to be.
# That derivation was only ever correct while no tier sold the concierge: under
# this ladder the concierge is precisely the difference between ``site`` and
# ``staff``, so deriving it would hand the $7 rung the $19 rung's feature.
# (The retired ``studio`` flat sold white-label hosting rather than staffing — the
# badge removal five times over, not the concierge — while ``agency`` did sell it,
# which is what its pooled conversation rate was for. Neither is in the catalog
# since 2026-09-06.) Unknown keys resolve False in ``_build``.
_SITE_PLAN_SELLS_CONCIERGE: dict[str, bool] = {
    "free": False,
    "site": False,
    "staff": True,
}

# Whether a tier may ship a site WITHOUT the attribution badge. ``free`` is the
# floor and keeps its badge — that is the whole difference between free and paid.
# Absent/unknown keys resolve False in ``_build``, so a typo means BADGED:
# fail-closed, matching ``sites.badge``'s posture everywhere else.
_SITE_PLAN_BADGE_REMOVAL: dict[str, bool] = {
    "free": False,
    "site": True,
    "staff": True,
}

# WHITE-LABEL AND INCLUDED-SITES ARE GONE FROM THIS CATALOG (2026-09-06).
#
# Both existed only to describe the ``studio``/``agency`` org flats, which are
# retired — the workspace plan carries sites now (Paw Go 1, Pro 3, Pro Max 10),
# so a second per-site ladder promising 5 or 25 sites for a flat fee contradicted
# it in the storefront and priced worse than Pro Max at every rung.
#
# ``included_sites`` in particular had to go rather than sit unread. The name is
# live again on ``billing.plans.PlanTier``, where it means the real thing, and one
# name meaning two things across two catalogs is how the wrong one gets wired up.

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
}

# The list rate is 10 cents, and after the 2026-09-06 retirement of the ``studio``
# and ``agency`` org flats EVERY tier carries it — the half-price pooled rate was
# ``agency``'s, and it left the catalog with the tier. The dict stays a dict rather
# than collapsing to the constant because the rate is a per-tier CLAIM: the day a
# rung is sold on a cheaper conversation, this is where it is priced, and a caller
# reading ``tier.conversation_rate_cents`` needs no edit.
#
# Tiers that sell no concierge still carry the list rate rather than 0 — a 0 here
# would read as "free conversations" to anything that renders it, which is the
# opposite of the truth (they get none).
_LIST_CONVERSATION_RATE_CENTS = 10
_SITE_PLAN_CONVERSATION_RATE_CENTS: dict[str, int] = {
    "free": _LIST_CONVERSATION_RATE_CENTS,
    "site": _LIST_CONVERSATION_RATE_CENTS,
    "staff": _LIST_CONVERSATION_RATE_CENTS,
}

# Buyer-facing name + the one line a plan card leads with. The catalog owns these
# rather than the frontend, for the reason the storefront's own comment gives: a
# blurb keyed on a tier name in the client says nothing the day the keys change,
# and the keys just changed. Mirrors ``billing.plans._PLAN_DISPLAY``.
_SITE_PLAN_DISPLAY: dict[str, tuple[str, str]] = {
    "free": ("Free", "Build and publish as many sites as you like, on a pawsites subdomain."),
    # "WATERMARK", NOT "BADGE", AND NOT "PAW BAR". This is card copy the client
    # renders verbatim, and it has to agree with the inclusion row beside it
    # ("Paw watermark removed", ``core/billing/site-plan-inclusions.ts``) — the
    # same thing under two names three inches apart is worse than either name.
    # "Paw Bar" would be actively wrong: that is the embeddable chat widget a
    # customer mounts on their own page, and paying for this rung does not
    # remove it. The mark here is ``sites.badge``'s attribution anchor.
    "site": ("Site", "Point your own domain at it and the Paw watermark comes off."),
    "staff": ("Staff", "Adds the visitor concierge — 200 conversations a month, then metered."),
}

# Extra selling points that are NOT capability flags, and the distinction matters
# enough to keep them in their own field. Every other entry in this catalog is
# something code can check; these are commitments a human honours. Putting SSO in
# ``_SITE_PLAN_CF_FEATURES`` would make it look enforced by something.
_SITE_PLAN_HIGHLIGHTS: dict[str, tuple[str, ...]] = {
    "free": (),
    "site": (),
    "staff": (),
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
# This field answers "how many domained sites does THIS SITE's own plan allow".
_SITE_PLAN_MAX_DOMAINED_SITES: dict[str, int | None] = {
    "free": 1,
    "site": None,
    "staff": None,
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
_SITE_TIER_ORDER: tuple[str, ...] = ("free", "site", "staff")

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
    ``cloudflare_features`` is the set of Cloudflare features the
    tier resells (BC-10 provisions them). ``badge_removal`` is whether a site on
    this tier may ship without the attribution badge — read by
    ``sites.badge.badge_required``. ``sells_concierge`` is whether the tier sells
    the visitor concierge at all. ``max_domained_sites`` is how many SITES in the
    workspace may carry a custom domain on this tier (None = uncapped) — the site,
    not the hostname, so apex + ``www`` on one site spend one.

    ``scope`` is ``"site"`` or ``"org"`` and decides which of the two billing
    shapes this row is. Read it before doing anything with ``key``.

    ``conversation_allowance`` and
    ``conversation_rate_cents`` are CATALOG CLAIMS — what the tier will sell. No
    seam gates on them, and ``SiteEntitlements`` deliberately does not carry them.
    They are here so a plan card can describe the ladder honestly; do not mistake
    one for a resolved permission.

    ``sells_concierge``, ``is_org_scoped`` and ``purchasable`` are derived, not
    stored (see the properties).
    """

    key: str
    monthly_price_usd: int
    cloudflare_features: frozenset[str]
    scope: str = ORG_SCOPE
    badge_removal: bool = False
    max_domained_sites: int | None = 0
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
        always succeeds. EVERY PRICED PER-SITE TIER IS ALSO PURCHASABLE NOW, and
        that is a change of meaning rather than a relaxation: this used to read
        "has a configured Dodo product" because a gateway was the only thing that
        could take money, so an unconfigured tier published live with no charge
        and left the site holding a paid ``plan_tier`` at
        ``subscription_status="none"`` — the free floor wearing a paid name.

        A paid site now bills against the workspace CREDIT WALLET, which exists on
        every deployment and needs no gateway configuration at all, so there is no
        longer a per-site tier that cannot be charged for. What an empty wallet
        produces is a clean 402 at purchase time, not a silent free grant — so
        "can this be bought" is answered by the ladder, and "can this be bought
        RIGHT NOW by this workspace" by the balance, which is the buyer's own
        business and not a property of the catalog.

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
        # No gateway test remains. The credit wallet is the rail every new
        # purchase takes and it needs no configuration, so a per-site rung is
        # buyable on its price alone. The two Dodo maps are still read elsewhere,
        # by the paths that renew the subscriptions already sold — they just no
        # longer decide whether anything can be sold.
        return True


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
        cloudflare_features=_SITE_PLAN_CF_FEATURES.get(key, frozenset()),
        scope=_SITE_PLAN_SCOPE.get(key, ORG_SCOPE),
        badge_removal=_SITE_PLAN_BADGE_REMOVAL.get(key, False),
        # ``.get(key, 0)`` and not ``.get(key)``: a missing key must mean NO
        # domains, while a present key mapped to None means UNCAPPED. Collapsing
        # the two would hand an unknown tier the uncapped answer.
        max_domained_sites=_SITE_PLAN_MAX_DOMAINED_SITES.get(key, 0),
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
    own", which lands it on the free floor.

    SINCE 2026-09-06 THIS RETURNS EXACTLY WHAT ``get_site_plan`` RETURNS, for every
    input. The catalog ships no org-scoped tier any more, and a RETIRED key is not
    a tier at all — ``canonical_site_tier_key("studio")`` is None, so the plain
    lookup already answers None and there is nothing left for the guard to catch.
    FOUR seams had a mutation swapping this call for ``get_site_plan``, and after
    the retirement all four ESCAPED — entitlements, ``add_domain``, the analytics
    gate, and the site-plan request door. That is the honest evidence, and the
    reason this paragraph replaced a claim that it still guarded them. Each of
    those mutations was repointed at a guard that still fires or, where the
    neighbouring mutations already covered what was left, removed. DO NOT RE-ADD
    ONE: an escape here is not a gap in the tests, it is this function being a
    synonym, and a plan with an escaping mutation reads as covered while proving
    nothing.

    It is kept anyway, and deliberately: it is the named seam every read of a
    ``Site.plan_tier`` goes through, so the day an org-scoped tier returns, the
    lookups that were safe do not have to be re-derived one call site at a time.
    Treat it as a shape the code holds, not as a live gate — the reachable half of
    the scope machinery is ``_build``'s ORG default for an unknown key, which IS
    tested and mutation-caught (``test_a_tier_the_catalog_does_not_know_is_built_org_scoped``).
    """
    tier = get_site_plan(key)
    if tier is None or tier.is_org_scoped:
        return None
    return tier


__all__ = [
    "ANALYTICS_FEATURE",
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
