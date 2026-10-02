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

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

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


class PartnerEarningsMonthOut(BaseModel):
    month: str  # "YYYY-MM", UTC
    sales: int
    revenue: list[PartnerMoneyOut]
    spent_credits: int
