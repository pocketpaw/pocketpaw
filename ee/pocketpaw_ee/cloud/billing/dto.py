# ee/pocketpaw_ee/cloud/billing/dto.py — request/response schemas for the
# billing HTTP surface (BC-2, the Gateway primitive).
#
# Distinct Request / Response DTOs per the EE cloud rule 4. The authenticated
# top-up endpoint (POST /billing/topup) takes a credit amount and hands back a
# hosted-checkout url. The public webhook endpoint reads RAW bytes (no DTO —
# the signature is over the exact bytes) and returns a tiny ack envelope.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateTopupRequest(BaseModel):
    """Body of ``POST /billing/topup`` — buy ``amount_credits`` of credits.

    ``amount_credits`` is integer credits (1 credit == $0.01); it must be a
    positive integer. The service validates again at entry (defence in depth).
    """

    amount_credits: int = Field(..., gt=0, description="Credits to buy (1 credit == $0.01).")


class CreateTopupResponse(BaseModel):
    """Hosted-checkout url the caller redirects the buyer to."""

    checkout_url: str


class WebhookAck(BaseModel):
    """Tiny ack the webhook endpoint returns on a 200."""

    ok: bool = True
    granted: bool = False
