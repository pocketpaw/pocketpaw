# ee/pocketpaw_ee/cloud/partners/dto.py — request/response shapes for Paw Partners.
#
# Requests and responses are separate classes (ee/cloud rule 4). The partner's
# own views (``PartnerMeOut`` and the client / offer / sale / earnings shapes)
# are served only on signed-in routes. ``PartnerPublicOut`` is the ONE shape an
# anonymous caller ever sees (directory and ``/partners/{slug}``): an allow-list
# with ``extra="forbid"``, never ``footer_name``, ``billing_country``,
# ``founding`` or ``status``. The operator write body lives in
# ``cloud/platform/partners.py``. Money is ISO-4217 minor units; GSTIN is
# upper-cased then pattern-checked; ``whatsapp_opt_in_at`` is stored as an ISO
# string and parsed back here.

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
)

from pocketpaw_ee.cloud.discover.dto import PublicListingResponse
from pocketpaw_ee.cloud.partners.domain import (
    PARTNER_SLUG_PATTERN,
    PartnerService,
    validate_partner_slug,
)

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
# ISO-3166 alpha-2, upper-cased first (the model's ``IsoCountry`` does the same).
Country = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=r"^[A-Z]{2}$")]
Slug = Annotated[
    str, StringConstraints(pattern=PARTNER_SLUG_PATTERN), AfterValidator(validate_partner_slug)
]
HttpsUrl = Annotated[str, StringConstraints(pattern=r"^https://", max_length=300)]
# Free-text names, stripped first so "  " cannot pass min_length and make a blank card.
Name80 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Name120 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


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
    """GET /partners/me and PATCH /partners/me/profile: the profile, where the
    partner stands (PH-15) and its public-profile fields (PW-7)."""

    # Sold sites (``partner_client_id`` set) with an active paid plan, whoever paid.
    active_sites: int
    # Distinct sites ever sold; never goes down (milestones count this).
    lifetime_sites_sold: int
    next_tier: PartnerNextTierOut | None
    benefits: PartnerBenefitsOut
    slug: str | None = None
    display_name: str | None = None
    city: str | None = None
    country: str | None = None
    services: list[str] = Field(default_factory=list)
    bio: str | None = None
    contact_url: str | None = None
    public: bool = False


class PartnerPublicProfileIn(BaseModel):
    """PATCH /partners/me/profile: only the fields sent are changed. The merged
    profile is validated as a whole by the service (reserved slug, slug
    uniqueness, ``public`` needs a slug and a display name)."""

    model_config = ConfigDict(extra="forbid")

    slug: Slug | None = None
    display_name: Name80 | None = None
    city: Name80 | None = None
    country: Country | None = None
    # ``null`` is a 422 here (the stored list is never null); send ``[]`` to clear.
    services: list[PartnerService] | None = Field(default=None, max_length=5)
    bio: str | None = Field(default=None, max_length=600)
    contact_url: HttpsUrl | None = None
    public: bool | None = None


class PartnerPublicOut(BaseModel):
    """One partner as anyone sees it. An allow-list: never ``footer_name``,
    ``billing_country``, ``founding`` or ``status``."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    display_name: str
    city: str | None
    country: str | None
    services: list[str]
    bio: str | None
    contact_url: str | None
    tier: str
    joined_at: datetime
    # The partner's newest public Discover listings (same card as the index),
    # capped at ``service_admin.SITES_PER_PARTNER`` (12); older ones are not listed.
    sites: list[PublicListingResponse]


class PartnerDirectoryPage(BaseModel):
    """A page of public partners, newest first; ``next_cursor`` is an opaque token
    (never a workspace id) to pass back as ``cursor``, None on the last page."""

    model_config = ConfigDict(extra="forbid")

    items: list[PartnerPublicOut]
    next_cursor: str | None = None


class PartnerApplyIn(BaseModel):
    """POST /partners/apply (public). Stored as one ``PartnerApplication`` for
    operators to review; ``turnstile_token`` is checked first (``_core.turnstile``)."""

    model_config = ConfigDict(extra="forbid")

    name: Name120
    email: EmailStr
    city: Name80
    country: Country
    services: list[PartnerService] = Field(min_length=1, max_length=5)
    message: str = Field(default="", max_length=2000)
    turnstile_token: str = Field(min_length=1, max_length=4096)


PartnerApplicationStatus = Literal["new", "contacted", "rejected", "accepted"]


class PartnerApplicationOut(BaseModel):
    """One application as an operator sees it (GET /platform/partners/applications).
    The applicant's contact details are here on purpose: this is the review queue."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    email: str
    city: str
    country: str
    services: list[str]
    message: str
    status: PartnerApplicationStatus
    note: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime


class PartnerApplicationPage(BaseModel):
    """A page of applications, newest first; ``next_cursor`` is None on the last page."""

    model_config = ConfigDict(extra="forbid")

    items: list[PartnerApplicationOut]
    next_cursor: str | None = None


class PartnerApplicationReviewIn(BaseModel):
    """PATCH /platform/partners/applications/{id}: the operator's decision.
    ``reason`` goes to the platform audit row, ``note`` stays on the application."""

    model_config = ConfigDict(extra="forbid")

    status: PartnerApplicationStatus
    note: str = Field(default="", max_length=2000)
    reason: str = ""


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
