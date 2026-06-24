# ee/pocketpaw_ee/cloud/billing/domain.py — frozen, framework-free value objects
# the billing provider abstraction hands back across its boundary (BC-2, the
# Gateway primitive).
#
# These are the normalized shapes EVERY payment provider (Dodo today; Razorpay
# later) returns, so the service / webhook layer never touches a Dodo-specific
# SDK type. A provider adapts its SDK's response into these; the rest of the
# billing subsystem only ever sees these two dataclasses.
#
#   * ``OneTimeCheckout`` — what ``create_one_time`` returns: the hosted-checkout
#     url the user is sent to, plus the gateway's own payment reference.
#   * ``GatewayEvent``    — a VERIFIED, normalized inbound webhook event. The
#     signature has already been checked before one of these exists.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OneTimeCheckout:
    """A created one-time-payment hosted checkout, normalized across gateways.

    ``checkout_url`` is the hosted page the buyer is redirected to.
    ``gateway_ref`` is the gateway's own id for the created payment (Dodo's
    ``payment_id``), kept for reconciliation / support lookups.
    """

    checkout_url: str
    gateway_ref: str


@dataclass(frozen=True)
class GatewayEvent:
    """A VERIFIED, normalized inbound webhook event.

    Only constructed AFTER the provider has verified the signature, so the
    fields can be trusted. ``event_id`` is the gateway's unique delivery id
    (Standard Webhooks ``webhook-id``) — it is the idempotency key the credit
    grant keys on so a replayed delivery grants EXACTLY ONCE. ``amount_credits``
    is integer credits (1 credit == $0.01); the provider converts the gateway's
    money amount back to credits. ``raw`` is the parsed event body, retained for
    audit / debugging (it carries no secret — the secret never enters the body).
    """

    event_id: str
    type: str
    amount_credits: int
    workspace_id: str
    currency: str
    raw: dict = field(default_factory=dict)
