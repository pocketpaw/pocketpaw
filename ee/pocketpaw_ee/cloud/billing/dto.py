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
