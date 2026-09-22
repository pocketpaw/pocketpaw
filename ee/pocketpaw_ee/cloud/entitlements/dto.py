# ee/pocketpaw_ee/cloud/entitlements/dto.py — request/response schemas for the
# entitlements + plan-catalog HTTP surface (BC-6, the Plan + Entitlement
# primitives).
#
# A READ-ONLY surface, so there are no request DTOs. Four responses:
#   * ``PlanCatalogResponse`` / ``PlanTierResponse`` — ``GET /billing/plans``, the
#     WORKSPACE plan ladder. Mirrors ``billing.plans.PlanTier``.
#   * ``EntitlementsResponse`` — ``GET /entitlements``, one workspace's RESOLVED
#     entitlements. Mirrors ``entitlements.domain.Entitlements``.
#   * ``SitePlanCatalogResponse`` / ``SitePlanTierResponse`` —
#     ``GET /billing/site-plans``, the PER-SITE ladder. Mirrors ``SitePlanTier``.
#
# INVARIANTS a reader must not break:
#   * FEATURE COLLECTIONS SERIALIZE SORTED. ``features`` and
#     ``cloudflare_features`` are sets server-side and sorted lists on the wire,
#     so a response is stable and diff-friendly. ``highlights`` is the exception
#     and must stay one: it is ordered card copy, so it is cast, not sorted.
#   * A CATALOG DTO SAYS WHAT A TIER SELLS; ``SiteEntitlementsResponse`` SAYS WHAT
#     A SITE MAY DO. Keep the asymmetry. ``conversation_allowance``,
#     ``conversation_rate_cents`` and ``white_label`` are unenforced ladder claims
#     and appear only on the catalog row, never as an entitlement.
#   * ``SitePlanTierResponse.scope`` IS REQUIRED, WITH NO DEFAULT, and that is the
#     point of it. The client filters its tier picker on this field, so a default
#     of ``"site"`` made a dropped ``scope=tier.scope`` in the mapper invisible —
#     every row still read "site" and an org-scoped tier would have been offered
#     for sale. Required turns that omission into a construction error.
#   * READ ``max_domained_sites``, NOT ``cloudflare_features``, to answer "does
#     this tier get a custom domain". The two disagree on the free floor and
#     always will: free carries one domained site beside an empty
#     ``cloudflare_features``, because that collection means only RESOLD
#     Cloudflare capability. ``site_domain_allowance`` — the gate that decides
#     whether an attach succeeds — reads the former.
#   * Prices are integers in their natural denomination and rates are CENTS.
#     $0.05 has no exact float representation and gets multiplied by a count.

from __future__ import annotations

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.billing.plans import PlanTier
from pocketpaw_ee.cloud.billing.site_plans import SitePlanTier
from pocketpaw_ee.cloud.entitlements.domain import Entitlements


class PlanTierResponse(BaseModel):
    """One row of the plan catalog on the wire — mirrors ``plans.PlanTier``.

    ``monthly_credit_allotment`` is integer credits (1 credit == $0.01) — a
    BACK-OFFICE field, NOT the headline. The UI renders ``usage_label`` (the
    ChatGPT/Claude-style "5x the usage" wording) + ``usage_detail`` instead, with
    ``display_name`` as the tier name and the INR/USD monthly+annual prices.
    ``enterprise`` prices arrive as null ("talk to us"). ``features`` is a sorted
    list (deterministic JSON). ``dodo_product_id`` is None until BC-7 / config
    populates it.
    """

    key: str
    monthly_credit_allotment: int
    dodo_product_id: str | None = None
    features: list[str] = Field(default_factory=list)
    display_name: str = ""
    usage_label: str = ""
    usage_detail: str = ""
    price_inr_monthly: int | None = None
    price_inr_annual: int | None = None
    price_usd_monthly: int | None = None
    price_usd_annual: int | None = None
    max_seats: int | None = None
    max_pockets: int | None = None
    max_connectors: int | None = None
    max_storage_bytes: int | None = None
    # How many Paw Sites this plan carries at ``staff`` quality. On the wire
    # because the pricing page renders it as headline copy — "3 sites included" is
    # what a buyer compares plans on now.
    included_sites: int | None = None


class PlanCatalogResponse(BaseModel):
    """The full plan catalog — response of ``GET /billing/plans``."""

    plans: list[PlanTierResponse] = Field(default_factory=list)


class EntitlementsResponse(BaseModel):
    """A workspace's resolved entitlements — response of ``GET /entitlements``.

    Mirrors ``entitlements.domain.Entitlements``. ``features`` is a sorted list.
    """

    workspace_id: str
    plan: str
    monthly_credit_allotment: int
    features: list[str] = Field(default_factory=list)
    max_seats: int | None = None
    max_pockets: int | None = None
    max_connectors: int | None = None
    max_storage_bytes: int | None = None
    # The site allowance the builder needs BEFORE a publish, to say whether the
    # next site is covered or costs credits. Reading it from the plan catalog on
    # the client would mean re-deriving the workspace's own tier there.
    included_sites: int | None = None
    # May this account read the source code of the sites it owns. On the wire so
    # the builder can hide the source view and NAME the reason, rather than
    # offering it and having the read refused. Defaults to False: a response
    # assembled without the field withholds source, matching the domain object's
    # own fail-closed default.
    site_source_visible: bool = False


def plan_tier_to_dto(tier: PlanTier) -> PlanTierResponse:
    """Map a frozen ``plans.PlanTier`` to its wire DTO (features sorted)."""
    return PlanTierResponse(
        key=tier.key,
        monthly_credit_allotment=tier.monthly_credit_allotment,
        dodo_product_id=tier.dodo_product_id,
        features=sorted(tier.features),
        display_name=tier.display_name,
        usage_label=tier.usage_label,
        usage_detail=tier.usage_detail,
        price_inr_monthly=tier.price_inr_monthly,
        price_inr_annual=tier.price_inr_annual,
        price_usd_monthly=tier.price_usd_monthly,
        price_usd_annual=tier.price_usd_annual,
        max_seats=tier.max_seats,
        max_pockets=tier.max_pockets,
        max_connectors=tier.max_connectors,
        max_storage_bytes=tier.max_storage_bytes,
        included_sites=tier.included_sites,
    )


def entitlements_to_dto(ent: Entitlements) -> EntitlementsResponse:
    """Map a frozen ``domain.Entitlements`` to its wire DTO (features sorted)."""
    return EntitlementsResponse(
        workspace_id=ent.workspace_id,
        plan=ent.plan,
        monthly_credit_allotment=ent.monthly_credit_allotment,
        features=sorted(ent.features),
        max_seats=ent.max_seats,
        max_pockets=ent.max_pockets,
        max_connectors=ent.max_connectors,
        max_storage_bytes=ent.max_storage_bytes,
        included_sites=ent.included_sites,
        site_source_visible=ent.site_source_visible,
    )


class SitePlanTierResponse(BaseModel):
    """One row of the PER-SITE plan catalog on the wire — mirrors ``SitePlanTier``.

    ``monthly_price_usd`` is the recurring MONTHLY sticker (USD, whole dollars).
    ``cloudflare_features`` is the SORTED list of Cloudflare features the tier
    resells (deterministic JSON; BC-10 provisions these when a domain is added).
    Carries NO gateway id: a per-site plan is paid from the workspace credit
    balance, so there is no product for the card to reference and nothing the
    frontend could do with one.

    ``purchasable`` is whether a customer can buy this tier at all: a $0 tier
    always can, a priced tier only once a Dodo recurring product is configured for
    it. False means the storefront should mark the tier unavailable rather than
    offer an upgrade button — selecting an unpurchasable paid tier publishes live,
    takes no money, and grants nothing, which reads to the buyer as a successful
    upgrade that silently did not work.

    ``badge_removal`` and ``sells_concierge`` are what the tier SELLS, not what any
    particular site has: they say a tier may drop the attribution badge and may run
    a visitor concierge. A site gets neither until its own subscription is active —
    that AND lives in ``resolve_site_entitlements``, and it is why these two must
    never be read as a per-site entitlement. They are here so a plan CARD can say
    what each tier includes and, by their absence, what it does not.

    ``scope`` is ``"site"`` on every tier the catalog ships, and is REQUIRED here
    with no default. That is the point of it: the client filters the picker on this
    field, so a tier arriving without one would be silently treated as per-site and
    offered for sale. A default of ``"site"`` made exactly that failure invisible —
    dropping ``scope=tier.scope`` from the mapper below was mutated in and ESCAPED
    the suite, because every row still read "site" from the default. Required means
    the omission is a construction error instead.

    (It is the CLIENT's filter this protects, not the backend's. ``site_scoped_tier``
    used to guard on scope too, but with the org flats retired it now returns
    whatever the plain lookup returns — see its docstring. The field is load-bearing
    for the picker and for whatever org-scoped tier the catalog grows next.)

    ``max_domained_sites`` is how many SITES in the workspace may carry a custom
    domain on this tier (``None`` = uncapped, ``0`` = none at all). THE UNIT IS
    THE SITE, NOT THE HOSTNAME — apex + ``www`` on one site spend one. READ THIS,
    not ``cloudflare_features``, to answer "does this tier get a custom domain".
    The two disagree on the FREE FLOOR and always will: free carries
    ``max_domained_sites=1`` (the captain's grant of 2026-08-21) beside an empty
    ``cloudflare_features``, because that collection means only RESOLD Cloudflare
    capability. ``site_domain_allowance`` — the gate that actually decides whether
    an attach succeeds — reads this field, so a card reading the other one
    promises something different from what the gate enforces.

    Unlike the two above it, this is NOT merely a ladder claim: it is the input to
    a live entitlement, which is why it is worth shipping rather than deriving.

    ``conversation_allowance`` and ``conversation_rate_cents`` describe the
    LADDER, not any site's permissions — they are here so a card can state what a
    tier sells. (``white_label`` and ``included_sites`` sat beside them until
    2026-09-06; both existed only for the retired org flats, and ``included_sites``
    now means something real on the WORKSPACE plan catalog instead.) ``SiteEntitlementsResponse`` —
    the read that answers "what may THIS site do" — deliberately carries none of
    them.

    ``conversation_rate_cents`` is cents, not dollars: $0.05 has no exact float
    representation and the number gets multiplied by a conversation count.

    ``display_name``, ``tagline`` and ``highlights`` are the card's copy, owned by
    the catalog rather than the client. A blurb keyed on a tier name in the
    frontend says nothing the day the keys change, and they just did.
    ``highlights`` are commitments a human honours (SSO, an SLA) rather than flags
    code checks — kept apart from ``cloudflare_features`` so they cannot be
    mistaken for something enforced.
    """

    key: str
    monthly_price_usd: int
    cloudflare_features: list[str] = Field(default_factory=list)
    # Defaults to 0 — "no domained sites" — matching ``SitePlanTier``'s own
    # default and failing closed the same way. A DTO built without the field (a
    # fixture, an older test) then claims nothing, rather than the "uncapped" a
    # ``None`` default would claim.
    max_domained_sites: int | None = 0
    scope: str
    conversation_allowance: int = 0
    conversation_rate_cents: int = 0
    display_name: str = ""
    tagline: str = ""
    highlights: list[str] = Field(default_factory=list)
    badge_removal: bool = False
    sells_concierge: bool = False
    purchasable: bool = True


class SitePlanCatalogResponse(BaseModel):
    """The full per-site plan catalog — response of ``GET /billing/site-plans``."""

    site_plans: list[SitePlanTierResponse] = Field(default_factory=list)


def site_plan_tier_to_dto(tier: SitePlanTier) -> SitePlanTierResponse:
    """Map a frozen ``site_plans.SitePlanTier`` to its wire DTO (features sorted)."""
    return SitePlanTierResponse(
        key=tier.key,
        monthly_price_usd=tier.monthly_price_usd,
        cloudflare_features=sorted(tier.cloudflare_features),
        max_domained_sites=tier.max_domained_sites,
        scope=tier.scope,
        conversation_allowance=tier.conversation_allowance,
        conversation_rate_cents=tier.conversation_rate_cents,
        display_name=tier.display_name,
        tagline=tier.tagline,
        # ``highlights`` is a tuple on the catalog row (frozen dataclass) and a
        # list on the wire. Order is meaningful and preserved — it is card copy,
        # not a set — so this is a cast, not the ``sorted(...)`` the feature
        # collections above get.
        highlights=list(tier.highlights),
        badge_removal=tier.badge_removal,
        sells_concierge=tier.sells_concierge,
        purchasable=tier.purchasable,
    )
