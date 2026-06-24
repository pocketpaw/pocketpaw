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

from __future__ import annotations

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.billing.plans import PlanTier
from pocketpaw_ee.cloud.billing.site_plans import SitePlanTier
from pocketpaw_ee.cloud.entitlements.domain import Entitlements


class PlanTierResponse(BaseModel):
    """One row of the plan catalog on the wire — mirrors ``plans.PlanTier``.

    ``monthly_credit_allotment`` is integer credits (1 credit == $0.01).
    ``features`` is a sorted list (deterministic JSON). ``dodo_product_id`` is
    None until BC-7 / config populates it.
    """

    key: str
    monthly_credit_allotment: int
    dodo_product_id: str | None = None
    features: list[str] = Field(default_factory=list)


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


def plan_tier_to_dto(tier: PlanTier) -> PlanTierResponse:
    """Map a frozen ``plans.PlanTier`` to its wire DTO (features sorted)."""
    return PlanTierResponse(
        key=tier.key,
        monthly_credit_allotment=tier.monthly_credit_allotment,
        dodo_product_id=tier.dodo_product_id,
        features=sorted(tier.features),
    )


def entitlements_to_dto(ent: Entitlements) -> EntitlementsResponse:
    """Map a frozen ``domain.Entitlements`` to its wire DTO (features sorted)."""
    return EntitlementsResponse(
        workspace_id=ent.workspace_id,
        plan=ent.plan,
        monthly_credit_allotment=ent.monthly_credit_allotment,
        features=sorted(ent.features),
    )


class SitePlanTierResponse(BaseModel):
    """One row of the PER-SITE plan catalog on the wire — mirrors ``SitePlanTier``.

    ``annual_price_usd`` is the recurring annual sticker (USD, whole dollars).
    ``cloudflare_features`` is the SORTED list of Cloudflare features the tier
    resells (deterministic JSON; BC-10 provisions these when a domain is added).
    ``dodo_product_id`` is None until config populates it.
    """

    key: str
    annual_price_usd: int
    dodo_product_id: str | None = None
    cloudflare_features: list[str] = Field(default_factory=list)


class SitePlanCatalogResponse(BaseModel):
    """The full per-site plan catalog — response of ``GET /billing/site-plans``."""

    site_plans: list[SitePlanTierResponse] = Field(default_factory=list)


def site_plan_tier_to_dto(tier: SitePlanTier) -> SitePlanTierResponse:
    """Map a frozen ``site_plans.SitePlanTier`` to its wire DTO (features sorted)."""
    return SitePlanTierResponse(
        key=tier.key,
        annual_price_usd=tier.annual_price_usd,
        dodo_product_id=tier.dodo_product_id,
        cloudflare_features=sorted(tier.cloudflare_features),
    )
