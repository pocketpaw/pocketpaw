# ee/pocketpaw_ee/cloud/billing/domain.py — frozen, framework-free value objects
# the billing provider abstraction hands back across its boundary (BC-2, the
# Gateway primitive; BC-7, the Subscription primitive).
#
# These are the normalized shapes EVERY payment provider (Dodo today; Razorpay
# later) returns, so the service / webhook layer never touches a Dodo-specific
# SDK type. A provider adapts its SDK's response into these; the rest of the
# billing subsystem only ever sees these dataclasses.
#
#   * ``OneTimeCheckout``      — what ``create_one_time`` returns: the hosted-
#     checkout url the user is sent to, plus the gateway's own payment reference.
#   * ``GatewayEvent``         — a VERIFIED, normalized inbound one-time webhook
#     event. The signature has already been checked before one of these exists.
#   * ``SubscriptionCheckout`` — what ``create_subscription`` returns: the hosted-
#     checkout url for a RECURRING checkout, plus the gateway's subscription ref.
#   * ``SubscriptionEvent``    — a VERIFIED, normalized inbound SUBSCRIPTION
#     webhook event (active / renewed / cancelled). Carries the tier (plan_key)
#     and the gateway subscription id so the service can grant the allotment and
#     update the workspace plan. Like ``GatewayEvent``, only built post-verify.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.
# Updated 2026-06-24 (BC-7): added ``SubscriptionCheckout`` + ``SubscriptionEvent``
#   for the recurring-subscription flow (subscribe + renewal credit grant).

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


@dataclass(frozen=True)
class SubscriptionCheckout:
    """A created RECURRING-subscription hosted checkout, normalized across gateways.

    ``checkout_url`` is the hosted page the buyer is redirected to to confirm the
    subscription. ``subscription_id`` is the gateway's own id for the created
    subscription (Dodo's ``subscription_id``), kept so a later cancel / change can
    address it and so the renewal webhook can be reconciled against it.
    """

    checkout_url: str
    subscription_id: str


@dataclass(frozen=True)
class SubscriptionEvent:
    """A VERIFIED, normalized inbound SUBSCRIPTION webhook event.

    Only constructed AFTER the provider has verified the signature, so the fields
    can be trusted. ``event_id`` is the gateway's unique delivery id (Standard
    Webhooks ``webhook-id``) — the idempotency key the per-renewal credit grant
    keys on, so a REPLAY of one renewal grants nothing while each NEW month's
    renewal (a fresh delivery id) grants again. ``type`` is the normalized event
    (``subscription.active`` | ``subscription.renewed`` | ``subscription.cancelled``).
    ``plan_key`` is the tier pulled from the subscription metadata (the catalog
    then yields the monthly allotment). ``product_id`` is the gateway's recurring-
    product id (the reverse-map fallback when metadata lacks ``plan_key``).
    ``subscription_id`` is the gateway subscription this event belongs to. ``raw``
    is the parsed body, retained for audit (it carries no secret).
    """

    event_id: str
    type: str
    workspace_id: str
    plan_key: str
    product_id: str
    subscription_id: str
    raw: dict = field(default_factory=dict)
