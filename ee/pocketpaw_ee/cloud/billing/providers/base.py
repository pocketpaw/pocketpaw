# ee/pocketpaw_ee/cloud/billing/providers/base.py — the payment-provider port
# (BC-2, the Gateway primitive).
#
# ``IPaymentsProvider`` is the ABC every gateway implements. It carries ONLY the
# BC-2 surface — a one-time top-up checkout and an inbound webhook verify+parse.
# Subscription methods are deliberately ABSENT: they land with the subscription
# task, not as speculative stubs here. The billing service depends only on this
# ABC, so swapping Dodo for Razorpay (or running both) never touches the service.
#
# Both methods speak in the framework-free ``domain`` value objects
# (``OneTimeCheckout`` / ``GatewayEvent``), never a vendor SDK type — that is the
# whole point of the port.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new module.

from __future__ import annotations

from abc import ABC, abstractmethod

from pocketpaw_ee.cloud.billing.domain import GatewayEvent, OneTimeCheckout


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
    ) -> GatewayEvent:
        """VERIFY the webhook signature, then parse it into a ``GatewayEvent``.

        The signature is checked FIRST, against the RAW ``payload`` bytes. On a
        bad / missing signature (or malformed headers / stale timestamp) the
        implementation RAISES — it never returns an unverified event. Only after
        verification passes is the body parsed and normalized. ``event_id`` is the
        gateway's unique delivery id (the idempotency key the grant uses).
        """
        raise NotImplementedError
