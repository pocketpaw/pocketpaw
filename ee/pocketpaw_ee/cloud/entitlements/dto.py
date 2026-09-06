# ee/pocketpaw_ee/cloud/entitlements/dto.py — request/response schemas for the
# entitlements + plan-catalog HTTP surface (BC-6, the Plan + Entitlement
# primitives).
#
# Read-only surface, so there is no request DTO. ``PlanTierResponse`` mirrors a
# ``billing.plans.PlanTier`` (one catalog row); ``PlanCatalogResponse`` wraps the
# list for ``GET /billing/plans``. ``EntitlementsResponse`` mirrors
# ``entitlements.domain.Entitlements`` for ``GET /entitlements``. ``features`` is
# serialized as a SORTED list (a JSON array is deterministic on the wire; a set
# is not), so the response is stable and diff-friendly for clients.
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.
# Updated 2026-06-24 (BC-10): added ``SitePlanTierResponse`` / ``SitePlanCatalogResponse``
#   for ``GET /billing/site-plans`` — the PER-SITE plan catalog (each tier ->
#   annual price + the Cloudflare features it resells). ``cloudflare_features`` is a
#   SORTED list (deterministic JSON, same rule as ``features`` above). The frontend
#   (BC-11) reads this to render the publish tier picker.
# Updated 2026-06-25 (feat/consumer-plan-ladder): ``PlanTierResponse`` now carries
#   the user-facing display + price fields (display_name, usage_label, usage_detail,
#   INR/USD monthly+annual prices) so the billing UI can render ChatGPT/Claude-style
#   "usage" wording instead of raw credits. ``monthly_credit_allotment`` stays as a
#   back-office field; ``enterprise`` prices serialize as null ("talk to us").
# Updated 2026-07-08 (feat/billing-smb-caps): both ``PlanTierResponse`` and
#   ``EntitlementsResponse`` now carry the SMB resource caps ``max_seats`` /
#   ``max_pockets`` / ``max_connectors`` (the enforced ceilings the plan ladder
#   added), so the plan cards can render real per-tier limits and the settings UI
#   can show a workspace's resolved caps. ``None`` == uncapped (Enterprise).
# Updated 2026-08-08 (feat/billing-storage-caps): both responses now also carry
#   ``max_storage_bytes`` (the workspace S3 storage cap in bytes; ``None`` ==
#   uncapped Enterprise) so the plan cards can render the storage limit and the
#   Settings storage page can show used vs cap.
# Updated 2026-08-19 (feat/site-plan-catalog-inclusions): ``SitePlanTierResponse``
#   now also carries ``badge_removal`` and ``sells_concierge``. Both existed
#   server-side and neither reached the wire, so the buyer-facing plan cards could
#   only list ``cloudflare_features`` — they showed nothing about the attribution
#   badge or the concierge, which are the two capabilities the paid tiers are
#   actually sold on. A card that cannot name what a tier includes cannot name what
#   it omits either, which is the half buyers ask about.
# Updated 2026-08-22 (feat/site-pricing-ladder): ``SitePlanTierResponse`` carries
#   the rest of the catalog row now that the catalog is the five-tier pricing spec
#   — ``scope`` (site rung vs org flat), the card copy the backend owns
#   (``display_name`` / ``tagline`` / ``highlights``), and the ladder facts a card
#   has to state to be comparable (``white_label``, ``included_sites``,
#   ``conversation_allowance``, ``conversation_rate_cents``). The storefront was
#   deriving its own per-key labels and blurbs client-side, which the rekey would
#   have blanked; owning the copy here means renaming a tier is one edit.
#   NOTE the asymmetry with ``SiteEntitlementsResponse``, and keep it: this DTO
#   describes what a TIER SELLS, that one describes what a SITE MAY DO. The
#   conversation meter and white-label are unenforced claims today and appear only
#   here, never there.

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

    ``scope`` is ``"site"`` or ``"org"`` and tells the storefront which of two
    completely different things a row is. A site-scoped row is bought one site at
    a time and its key is a legal ``Site.plan_tier``; an org-scoped flat covers a
    whole workspace and its key must never be sent back as a publish's
    ``site_plan_key``. Org rows always arrive with ``purchasable: false`` because
    no org checkout exists yet — render them as "talk to us", never as a buy
    button.

    ``white_label``, ``included_sites``, ``conversation_allowance`` and
    ``conversation_rate_cents`` describe the LADDER, not any site's permissions.
    Nothing meters a conversation yet and nothing enforces white-label; they are
    here so a card can state what a tier will sell. ``SiteEntitlementsResponse`` —
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
    scope: str = "site"
    white_label: bool = False
    included_sites: int | None = None
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
        scope=tier.scope,
        white_label=tier.white_label,
        included_sites=tier.included_sites,
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
