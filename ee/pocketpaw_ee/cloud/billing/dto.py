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
#   for ``GET /billing/usage`` — daily usage by model over a date range. The backend
#   stays DAILY (the frontend aggregates weekly/monthly + filters); spend is reported
#   in CREDITS (the same denomination the rest of billing uses).
# Updated 2026-06-29 (fix/billing-usage-ledger-source): the usage graph is now
#   sourced from the workspace's CREDIT LEDGER (the wallet's own meter), not the
#   LiteLLM proxy — the contract SHAPE is unchanged (the frontend is untouched), but
#   ``UsageModelStats.tokens`` is 0 from this source (the ledger carries no per-entry
#   token count). The credit + request figures are accurate.
# Updated 2026-07-11 (feat/llm-cost-attribution): ``UsageModelStats.tokens`` now
#   carries REAL token volume — the metering path stamps ``total_tokens`` on each
#   debit ref and the ledger read sums it. The contract SHAPE is still unchanged
#   (the frontend is untouched). Also tightened the field descriptions so the spend
#   figures read unambiguously as CREDITS (1 credit == $0.01), never USD.
# Updated 2026-07-08 (feat/billing-cancel-downgrade): added
#   ``CancelSubscriptionResponse`` for ``POST /billing/cancel`` (no request body —
#   the caller's workspace is the scope). Tiny ack; the plan revert is not reflected
#   here (it lands on the ``subscription.cancelled`` webhook).
# Updated 2026-09-11 (fix/billing-usage-chart-micro): the usage contract gained a
#   MICRO-CREDIT figure at every level — ``UsageModelStats.credits_micro``,
#   ``UsageBucket.total_credits_micro``, ``WorkspaceUsageResponse.total_credits_micro``.
#   The whole-credit fields were all that existed, and since the micro migration a
#   chat run costs about 375_000 micro (0 whole credits), so a customer with light
#   usage read a chart of zeros beside a wallet they could watch draining. Same fix
#   PR #2071 made for the history rows (``LedgerEntryResponse.amount_delta_micro``);
#   the chart did not get it at the time. Additive — the existing fields keep their
#   names and meaning, so no client breaks.

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


class CancelSubscriptionResponse(BaseModel):
    """Ack for ``POST /billing/cancel`` — the gateway was told to stop billing.

    ``ok`` is True once the gateway cancel was requested. The plan revert
    (``Workspace.plan`` -> free) is NOT reflected here — it lands reactively when
    Dodo posts the verified ``subscription.cancelled`` webhook.
    """

    ok: bool = True


class WebhookAck(BaseModel):
    """Tiny ack the webhook endpoint returns on a 200."""

    ok: bool = True
    granted: bool = False


# ---------------------------------------------------------------------------
# Workspace usage graph (GET /billing/usage)
# ---------------------------------------------------------------------------


class UsageModelStats(BaseModel):
    """One model's usage within a single day.

    ``credits_micro`` is the day-and-model spend in MICRO-CREDITS (1_000_000 micro
    == 1 credit == $0.01) straight off the wallet's ledger, markup already applied
    at debit time. **Render from it.** ``credits`` is the same figure truncated to
    whole credits and it is not sufficient on its own: a chat run costs about
    $0.0015 of compute, which is 375_000 micro and 0 whole credits, so a day of
    ordinary light usage arrives here as ``credits: 0`` for every model while the
    wallet visibly drains. (This is the chart's version of the ``amount_delta: 0``
    debits observed on the history rows on 2026-09-04.)

    ``tokens`` is the real total token volume for this model (summed from each
    debit's ``ref.total_tokens``; a legacy debit written before the ref carried
    tokens contributes 0); ``requests`` is the count of charged ledger entries for
    this model on this day. A model whose spend is under a credit still appears —
    usage is real even when the whole-credit figure is 0.
    """

    credits: int = Field(
        ...,
        description=(
            "Spend for this model on this day, truncated to whole CREDITS "
            "(1 credit == $0.01; not USD). 0 for sub-credit spend — read "
            "credits_micro to render it."
        ),
    )
    # Exact, positive, in micro-credits. The whole-credit field above cannot
    # express a sub-cent day, which is most days for a light workspace.
    credits_micro: int = Field(
        default=0,
        description=(
            "Exact spend for this model on this day, in MICRO-CREDITS "
            "(1_000_000 == 1 credit == $0.01). Render the chart from this."
        ),
    )
    tokens: int = Field(
        ...,
        description="Total token volume for this model on this day (0 for pre-attribution debits).",
    )
    requests: int = Field(..., description="Charged request count for this model on this day.")


class UsageBucket(BaseModel):
    """One day's usage: the per-model breakdown plus the day's total.

    ``date`` is ``YYYY-MM-DD``. ``by_model`` maps a model id to its stats for that
    day. ``total_credits_micro`` is the exact sum of the bucket's per-model
    ``credits_micro``; ``total_credits`` is that total truncated ONCE to whole
    credits. It is deliberately not the sum of the per-model ``credits`` — summing
    already-truncated figures compounds the shortfall across every model on the
    day, so three models at 750_000 micro each would report 0 for a day the wallet
    was charged two credits for.
    """

    date: str = Field(..., description="The bucket day, YYYY-MM-DD.")
    by_model: dict[str, UsageModelStats] = Field(
        default_factory=dict, description="Model id -> usage stats for this day."
    )
    total_credits: int = Field(
        ...,
        description=(
            "This day's spend truncated to whole CREDITS (not USD). Derived from "
            "total_credits_micro, never summed from the per-model credits."
        ),
    )
    total_credits_micro: int = Field(
        default=0,
        description="Exact total spend for this day, in MICRO-CREDITS (1_000_000 == 1 credit).",
    )


class WorkspaceUsageResponse(BaseModel):
    """Per-workspace daily usage over a date range, broken down by model.

    ``start_date`` / ``end_date`` echo the resolved window (``YYYY-MM-DD``; defaults
    to the last 30 days when the request omits them). ``models`` is the sorted
    distinct set of model ids seen across the whole range (so the frontend can build
    a stable legend / color map). ``buckets`` is one entry per day WITH usage, oldest
    first. ``total_credits_micro`` is the exact grand total over every bucket, in
    MICRO-CREDITS; ``total_credits`` is that figure truncated ONCE to whole credits
    (1 credit == $0.01 — NOT USD), never a sum of the per-bucket totals. The shape
    is kept DAILY on purpose — the frontend aggregates to weekly / monthly and
    filters by model client-side; it must aggregate the MICRO fields and convert at
    the end, for the same reason this response does. A workspace with no spend in
    the window yields empty ``models`` + ``buckets`` and both totals 0 (HTTP 200).
    """

    start_date: str
    end_date: str
    models: list[str] = Field(default_factory=list)
    buckets: list[UsageBucket] = Field(default_factory=list)
    total_credits: int = 0
    # Exact. Aggregate from this; total_credits cannot express a sub-credit window.
    total_credits_micro: int = 0
