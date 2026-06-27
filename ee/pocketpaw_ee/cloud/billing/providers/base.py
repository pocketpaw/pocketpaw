# ee/pocketpaw_ee/cloud/billing/providers/base.py — the payment-provider port
# (BC-2, the Gateway primitive; BC-7, the Subscription primitive).
#
# ``IPaymentsProvider`` is the ABC every gateway implements. It carries the BC-2
# surface — a one-time top-up checkout and an inbound webhook verify+parse — plus
# the BC-7 RECURRING-subscription surface: open a subscription checkout, cancel a
# subscription, and parse a verified subscription webhook into a normalized event.
# The billing service depends only on this ABC, so swapping Dodo for Razorpay (or
# running both) never touches the service.
#
# Every method speaks in the framework-free ``domain`` value objects
# (``OneTimeCheckout`` / ``GatewayEvent`` / ``SubscriptionCheckout`` /
# ``SubscriptionEvent``), never a vendor SDK type — that is the whole point of the
# port. The single ``verify_and_parse_webhook`` handles BOTH one-time AND
# subscription deliveries (the signature check is the same), returning whichever
# normalized event the delivery's ``type`` indicates.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new module.
# Updated 2026-06-24 (security): name the bad-signature exception in the port
#   docstring — ``BadRequest`` (400), not ``ValidationError`` (422).
# Updated 2026-06-24 (BC-7): added the subscription surface —
#   ``create_subscription`` + ``cancel_subscription``, and documented that
#   ``verify_and_parse_webhook`` now also returns a ``SubscriptionEvent`` for a
#   verified subscription.* delivery.

from __future__ import annotations

from abc import ABC, abstractmethod

from pocketpaw_ee.cloud.billing.domain import (
    GatewayEvent,
    OneTimeCheckout,
    SubscriptionCheckout,
    SubscriptionEvent,
)


class IPaymentsProvider(ABC):
    """Port for a one-time-payment gateway (Dodo in v1; Razorpay later).

    Implementations adapt their SDK to the normalized ``domain`` shapes so the
    rest of the billing subsystem stays gateway-agnostic.
    """

    @abstractmethod
    async def create_one_time(
        self,
        *,
        amount_credits: int,
        workspace_id: str,
        customer_email: str | None,
        metadata: dict,
    ) -> OneTimeCheckout:
        """Create a one-time payment and return its hosted checkout.

        ``amount_credits`` is integer credits (1 credit == $0.01). The provider
        converts to the gateway's money amount, attaches ``metadata`` (which MUST
        carry ``workspace_id`` so the webhook can route the grant), and returns
        the hosted ``checkout_url`` plus the ``gateway_ref``.
        """
        raise NotImplementedError

    @abstractmethod
    def verify_and_parse_webhook(
        self,
        *,
        payload: bytes,
        headers: dict[str, str],
    ) -> GatewayEvent | SubscriptionEvent:
        """VERIFY the webhook signature, then parse it into a normalized event.

        The signature is checked FIRST, against the RAW ``payload`` bytes. On a
        bad / missing signature (or malformed headers / stale timestamp) the
        implementation RAISES ``BadRequest`` (→ 400, an untrusted request — not
        ``ValidationError`` / 422) — it never returns an unverified event. Only
        after verification passes is the body parsed and normalized. ``event_id``
        is the gateway's unique delivery id (the idempotency key the grant uses).

        Returns a ``GatewayEvent`` for a one-time payment delivery (``payment.*``)
        or a ``SubscriptionEvent`` for a recurring-subscription delivery
        (``subscription.*``). The service routes on the returned type.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Subscriptions (BC-7)
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def create_subscription(
        self,
        *,
        plan_key: str,
        product_id: str,
        workspace_id: str,
        customer_email: str | None,
        metadata: dict,
    ) -> SubscriptionCheckout:
        """Create a RECURRING subscription and return its hosted checkout.

        ``product_id`` is the gateway's recurring-product id for the tier
        (resolved by the service from config). The provider attaches ``metadata``
        — which MUST carry ``workspace_id`` AND ``plan_key`` so the renewal
        webhook can route the grant back to the right wallet at the right tier —
        and returns the hosted ``checkout_url`` plus the gateway ``subscription_id``.
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel_subscription(self, subscription_id: str) -> None:
        """Cancel the gateway subscription identified by ``subscription_id``.

        Idempotent from the caller's view — cancelling an already-cancelled
        subscription should not raise a hard error. The entitlement revert
        (``Workspace.plan`` → free) is driven by the resulting cancellation
        webhook, NOT here, so this method only tells the gateway to stop billing.
        """
        raise NotImplementedError
