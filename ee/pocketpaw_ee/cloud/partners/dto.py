# ee/pocketpaw_ee/cloud/partners/dto.py — request/response shapes for Paw Partners.
# Created 2026-10-01 (feat/partners-foundation, PH-1). Requests and responses are
# separate classes (ee/cloud rule 4). Client wire shape unchanged by the Fabric
# move; ``whatsapp_opt_in_at`` is stored as an ISO string and parsed back here.
# Updated 2026-10-02: the profile write body moved to ``cloud/platform/partners.py``;
# GSTIN is upper-cased and pattern-validated.
# Updated 2026-10-02 (feat/partners-sell, PH-2): offer / sell / sold-site shapes.
# Updated 2026-10-02 (feat/partners-earnings, PH-11): the sell body takes an optional
# ``price_minor`` + ``currency`` (what the partner charged its client, validated
# BEFORE the wallet is touched); the sale returns ``invoice_id``; summary and
# monthly-earnings response shapes.
# Updated 2026-10-02 (feat/partners-commissions, PH-13): client pay link request /
# response; offers carry the client's list price (``client_price_minor`` +
# ``client_currency``); sold sites carry ``billing_mode`` ("partner" = the wallet
# paid, "client" = the client paid a link); summary and earnings carry the
# commission credits, net of clawbacks.
# Updated 2026-10-02 (feat/partners-tiers, PH-15): ``PartnerMeOut`` (the profile
# plus volume-tier standing: active / lifetime sites, next tier, benefits) for
# GET /partners/me — the platform route keeps the plain ``PartnerProfileOut``;
# summary and earnings carry milestone reward credits; ``PartnerRewardOut`` is
# one rung of the reward ladder.

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

E164 = r"^\+[1-9]\d{7,14}$"
GSTIN = r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
# Same ceiling as ``sites.dto.SiteInvoiceCreate.amount_cents``.
MAX_MINOR = 1_000_000_000_000


def _upper(v: Any) -> Any:
    return v.strip().upper() if isinstance(v, str) else v


# Indian GST number: upper-cased first, then the 15-char structural pattern.
Gstin = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=GSTIN)]
# ISO-4217-shaped code, upper-cased first (same rule as the site client receipts).
Currency = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=r"^[A-Z]{3}$")]


class PartnerProfileOut(BaseModel):
    status: str
    tier: str
    footer_name: str
    billing_country: str
    founding: bool
    joined_at: datetime


class PartnerNextTierOut(BaseModel):
    name: str
    at: int  # active sold sites the tier needs
    remaining: int


class PartnerBenefitsOut(BaseModel):
    # Off the wholesale partner price (the offers already show it applied).
    wholesale_discount_pct: float
    # The tier's commission rate. A founding partner's site earns
    # max(40%, this) for 24 months from that site's first client payment.
    commission_pct: float


class PartnerMeOut(PartnerProfileOut):
    """GET /partners/me: the profile plus where the partner stands (PH-15)."""

    # Sold sites (``partner_client_id`` set) with an active paid plan, whoever paid.
    active_sites: int
    # Distinct sites ever sold; never goes down (milestones count this).
    lifetime_sites_sold: int
    next_tier: PartnerNextTierOut | None
    benefits: PartnerBenefitsOut


class PartnerRewardOut(BaseModel):
    """One milestone rung: ``credits`` once the ``sites``-th distinct site is sold."""

    sites: int
    credits: int
    reached_at: datetime | None  # when the reward was credited; None = not yet


class PartnerClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    whatsapp: str = Field(pattern=E164)
    whatsapp_opt_in_at: datetime | None = None
    gstin: Gstin | None = None
    notes: str = Field(default="", max_length=5000)


class PartnerClientUpdateRequest(BaseModel):
    """PATCH: only the fields sent are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    whatsapp: str | None = Field(default=None, pattern=E164)
    whatsapp_opt_in_at: datetime | None = None
    gstin: Gstin | None = None
    notes: str | None = Field(default=None, max_length=5000)


class PartnerClientOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    whatsapp: str
    whatsapp_opt_in_at: datetime | None
    gstin: str | None
    notes: str
    created_at: datetime | None
    updated_at: datetime | None


class PartnerOfferOut(BaseModel):
    """One partner-only plan at the caller's country price (1 credit = $0.01)."""

    sku: str
    period_months: int
    price_credits: int
    conversation_allowance: int
    label: str
    # What the partner's client pays us for this plan through a pay link (list
    # price), in ISO-4217 minor units of ``client_currency``.
    client_price_minor: int
    client_currency: str


class PartnerSellRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=200)
    site_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=64)
    # What the partner charged its client, ISO-4217 minor units. Private to the
    # partner: it lands as a paid receipt on the site's client record, never on
    # anything the client sees. Omitted = nothing recorded.
    price_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR)
    # Defaults to INR for an IN partner, else USD.
    currency: Currency | None = None


class PartnerPayLinkRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=200)
    site_id: str = Field(min_length=1, max_length=64)
    sku: Literal["site_year", "staff_year"]


class PartnerPayLinkOut(BaseModel):
    checkout_url: str
    site_id: str
    sku: str
    amount_minor: int
    currency: str


class PartnerSaleOut(BaseModel):
    site_id: str
    name: str
    url: str
    plan_tier: str | None
    renewal_date: datetime | None
    partner_client_id: str | None
    subscription_status: str
    invoice_id: str | None = None


class PartnerSiteOut(BaseModel):
    site_id: str
    name: str
    url: str
    plan_tier: str | None
    renewal_date: datetime | None
    partner_client_id: str | None
    client_name: str
    # "client" when the client paid this site's year through a pay link, else
    # "partner" (the partner's wallet bought it).
    billing_mode: Literal["partner", "client"]


class PartnerMoneyOut(BaseModel):
    """An amount in ISO-4217 minor units of ``currency``. Never summed across currencies."""

    currency: str
    amount_minor: int


class PartnerSummaryOut(BaseModel):
    clients: int
    sites_sold: int
    active_sites: int
    renewals_due_30d: int
    spent_credits_30d: int
    spent_credits_total: int
    revenue_30d: list[PartnerMoneyOut]
    revenue_total: list[PartnerMoneyOut]
    # Credits earned on client payments, net of clawbacks (1 credit = $0.01).
    commission_credits_30d: int
    commission_credits_total: int
    # One-time milestone rewards (PH-15), credits.
    rewards_credits_30d: int
    rewards_credits_total: int
    lifetime_sites_sold: int


class PartnerEarningsMonthOut(BaseModel):
    month: str  # "YYYY-MM", UTC
    sales: int
    revenue: list[PartnerMoneyOut]
    spent_credits: int
    commission_credits: int
    rewards_credits: int
