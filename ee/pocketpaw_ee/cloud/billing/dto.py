# ee/pocketpaw_ee/cloud/billing/dto.py — request/response schemas for the
# billing HTTP surface (BC-2, the Gateway primitive).
#
# Distinct Request / Response DTOs per the EE cloud rule 4. The authenticated
# top-up endpoint (POST /billing/topup) takes a credit amount and hands back a
# hosted-checkout url. The public webhook endpoint reads RAW bytes (no DTO —
# the signature is over the exact bytes) and returns a tiny ack envelope.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.
# Updated 2026-06-24 (BC-7): added ``CreateSubscriptionRequest`` /
#   ``CreateSubscriptionResponse`` for ``POST /billing/subscribe`` — open a
#   recurring checkout for a plan tier. Same hosted-url-out shape as top-up.
# Updated 2026-06-24 (S1 review fix): ``CreateTopupRequest.amount_credits`` now
#   carries an upper bound (``le=1_000_000`` == $10,000). Without a ceiling a
#   typo'd / hostile amount could open a checkout for an absurd sum; over-ceiling
#   now 422s at the DTO before the service is reached.
# Updated 2026-06-29 (feat/billing-usage-endpoint): added the WORKSPACE USAGE
#   response contract (``UsageModelStats`` / ``UsageBucket`` / ``WorkspaceUsageResponse``)
#   for ``GET /billing/usage`` — daily usage by model over a date range, derived
#   from the workspace's LiteLLM proxy usage. The backend stays DAILY (the frontend
#   aggregates weekly/monthly + filters); spend is reported in CREDITS (the same
#   denomination the rest of billing uses).

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateTopupRequest(BaseModel):
    """Body of ``POST /billing/topup`` — buy ``amount_credits`` of credits.

    ``amount_credits`` is integer credits (1 credit == $0.01); it must be a
    positive integer at or below the ceiling. The service validates again at
    entry (defence in depth).
    """

    amount_credits: int = Field(
        ...,
        gt=0,
        le=1_000_000,
        description="Credits to buy (1 credit == $0.01). Capped at 1,000,000 credits ($10,000).",
    )


class CreateTopupResponse(BaseModel):
    """Hosted-checkout url the caller redirects the buyer to."""

    checkout_url: str


class CreateSubscriptionRequest(BaseModel):
    """Body of ``POST /billing/subscribe`` — subscribe to ``plan_key``.

    ``plan_key`` is a plan-catalog tier key (e.g. ``team`` / ``business`` /
    ``enterprise``). The service validates it against the catalog and resolves the
    Dodo recurring product before opening a checkout.
    """

    plan_key: str = Field(..., min_length=1, description="Plan tier key to subscribe to.")


class CreateSubscriptionResponse(BaseModel):
    """Hosted recurring-checkout url the caller redirects the buyer to."""

    checkout_url: str


class WebhookAck(BaseModel):
    """Tiny ack the webhook endpoint returns on a 200."""

    ok: bool = True
    granted: bool = False


# ---------------------------------------------------------------------------
# Workspace usage graph (GET /billing/usage)
# ---------------------------------------------------------------------------


class UsageModelStats(BaseModel):
    """One model's usage within a single day.

    ``credits`` is the day-and-model spend converted to integer credits (the same
    USD->credits conversion the meter uses: ``round(cost_usd * markup / credit_usd)``,
    1 credit == $0.01); ``tokens`` is total tokens (prompt + completion); ``requests``
    is the count of API requests. A model whose rounded credit cost is 0 (sub-credit
    spend) still appears — usage is real even when the rounded credit cost is 0.
    """

    credits: int = Field(..., description="Spend for this model on this day, in credits.")
    tokens: int = Field(..., description="Total tokens (prompt + completion) for this model.")
    requests: int = Field(..., description="API request count for this model on this day.")


class UsageBucket(BaseModel):
    """One day's usage: the per-model breakdown plus the day's credit total.

    ``date`` is ``YYYY-MM-DD``. ``by_model`` maps a model id to its stats for that
    day; ``total_credits`` is the sum of the bucket's per-model credits.
    """

    date: str = Field(..., description="The bucket day, YYYY-MM-DD.")
    by_model: dict[str, UsageModelStats] = Field(
        default_factory=dict, description="Model id -> usage stats for this day."
    )
    total_credits: int = Field(..., description="Sum of this day's per-model credits.")


class WorkspaceUsageResponse(BaseModel):
    """Per-workspace daily usage over a date range, broken down by model.

    ``start_date`` / ``end_date`` echo the resolved window (``YYYY-MM-DD``; defaults
    to the last 30 days when the request omits them). ``models`` is the sorted
    distinct set of model ids seen across the whole range (so the frontend can build
    a stable legend / color map). ``buckets`` is one entry per day WITH usage, oldest
    first. ``total_credits`` is the grand total over every bucket. The shape is kept
    DAILY on purpose — the frontend aggregates to weekly / monthly and filters by
    model client-side. A brand-new workspace with no usage (or no provisioned key)
    yields empty ``models`` + ``buckets`` and ``total_credits`` 0 (HTTP 200).
    """

    start_date: str
    end_date: str
    models: list[str] = Field(default_factory=list)
    buckets: list[UsageBucket] = Field(default_factory=list)
    total_credits: int = 0
