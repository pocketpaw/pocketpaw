# ee/pocketpaw_ee/cloud/billing/providers/dodo.py — the Dodo Payments
# implementation of ``IPaymentsProvider`` (BC-2, the Gateway primitive).
#
# Two responsibilities, both adapting the real SDK to the framework-free
# ``domain`` shapes:
#
#   * ``create_one_time`` — builds an async ``DodoPayments`` client from settings
#     and calls ``payments.create(...)`` with ``payment_link=True`` to get a
#     HOSTED checkout url back, ``metadata={"workspace_id": ...}`` so the webhook
#     can route the grant, and a pay-what-you-want cart line on the configured
#     credit product whose ``amount`` == the purchased credits (1 credit == $0.01
#     == 1 cent == the currency's lowest denomination, so amount_credits maps
#     1:1 onto Dodo's cents — NO division).
#
#   * ``verify_and_parse_webhook`` — verifies the Standard-Webhooks signature
#     with ``standardwebhooks.Webhook(...).verify(payload, headers)`` (raises on a
#     bad/missing signature, malformed headers, or a stale timestamp) BEFORE the
#     body is trusted, then normalizes the verified event into a ``GatewayEvent``.
#     ``event_id`` is the Standard-Webhooks ``webhook-id`` header (the idempotency
#     key); ``workspace_id`` is pulled from the payment's ``metadata``.
#
# SDK-API NOTES (discovered by reading the installed packages, NOT the brief):
#   * ``standardwebhooks.Webhook`` takes ``whsecret=`` (it strips a ``whsec_``
#     prefix and base64-decodes), NOT ``base64_secret=`` as the brief stated.
#   * Dodo's ``payments.create`` / ``checkout_sessions.create`` do NOT accept a
#     raw amount — they require a ``product_cart`` of pre-existing Dodo product
#     ids plus ``billing`` (country) and ``customer`` (email). A one-time top-up
#     for an arbitrary credit amount is therefore modeled as a pay-what-you-want
#     line on a configured credits product (``dodo_credit_product_id``), with the
#     amount carried on the cart item.
#   * The ``billing`` country defaults to ``US`` but is configurable via
#     ``dodo_billing_country`` (POCKETPAW_DODO_BILLING_COUNTRY) — set ``IN`` with
#     INR products to surface UPI; the buyer can still change it on the hosted page.
#   * The hosted url comes back as ``response.payment_link`` (only populated when
#     ``payment_link=True``); ``response.payment_id`` is the gateway ref.
#   * The webhook body is ``{business_id, data: Payment, timestamp, type}``; the
#     buyer's metadata lives at ``data.metadata`` and the money amount at
#     ``data.total_amount`` (lowest denomination) with ``data.currency``.
#
# SUBSCRIPTIONS (BC-7), again adapting the real SDK:
#
#   * ``create_subscription`` — opens a CHECKOUT SESSION via
#     ``client.checkout_sessions.create(...)``: a recurring ``product_cart`` line
#     ([{product_id, quantity:1}]) creates the SUBSCRIPTION at payment time,
#     ``return_url`` / ``cancel_url`` send the buyer back to the app afterward,
#     ``customer`` carries the email, ``billing_address.country`` keeps the
#     configurable billing country (IN+INR → UPI), and ``metadata={"workspace_id",
#     "plan_key"}`` lets the renewal webhook route the grant. The response is a
#     SESSION: the hosted url is ``response.checkout_url`` and ``response.session_id``
#     is carried on ``SubscriptionCheckout.subscription_id`` (the real gateway
#     subscription id is created at payment and arrives on the subscription.active
#     webhook body, NOT from this create call).
#
#     WHY checkout sessions (the fix): the prior ``subscriptions.create(
#     payment_link=True)`` call accepted NO return_url, so after paying on Dodo the
#     buyer was stranded — the app never got them back. Checkout Sessions is the
#     Dodo API that supports return_url + cancel_url.
#
#   * ``cancel_subscription`` — calls ``client.subscriptions.update(<id>,
#     status="cancelled")`` (the SDK has no ``.cancel`` — cancellation is a status
#     PATCH). The entitlement revert is driven by the resulting webhook, not here.
#
#   * ``verify_and_parse_webhook`` ALSO recognizes the subscription event family.
#     The real Dodo event names (verified against the SDK's WebhookEventType
#     literal in ``types/webhook_event_type.py``) are ``subscription.active``,
#     ``subscription.renewed``, ``subscription.cancelled`` (among others). The
#     event body is ``{business_id, data: Subscription, timestamp, type}``; the
#     buyer's metadata lives at ``data.metadata`` (carrying ``workspace_id`` +
#     ``plan_key``), the recurring product at ``data.product_id`` (the reverse-map
#     fallback for the tier), and the subscription id at ``data.subscription_id``.
#
# SECURITY: the signature is verified before the payload is parsed. The webhook
# secret and the API key are NEVER logged.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new module.
# Updated 2026-06-24 (security): warn (don't silently swallow) when a
#   payment.succeeded event carries a non-int / missing ``total_amount`` — the
#   safe-coerce-to-0 behavior is unchanged, but ops now get a log to triage.
# Updated 2026-06-24 (BC-7): implemented the subscription surface
#   (``create_subscription`` + ``cancel_subscription``) over
#   ``client.subscriptions.*``, and extended ``verify_and_parse_webhook`` to
#   return a ``SubscriptionEvent`` for a verified ``subscription.*`` delivery
#   (plan_key from metadata, with a product_id reverse-map fallback off the
#   configured plan->product mapping).
# Updated 2026-06-24 (BC-9): ``verify_and_parse_webhook`` now also reads
#   ``data.metadata.site_id`` onto the ``SubscriptionEvent`` — the discriminator
#   that tells a PER-SITE annual sub (each published site has its own plan) from a
#   WORKSPACE-plan sub. A per-site subscribe stamps ``site_id`` on the metadata;
#   the service routes a delivery with one to the site, without one to the
#   workspace path. No new SDK call — site subs reuse ``create_subscription``.
# Updated 2026-06-28 (fix/billing-checkout-sessions): ``create_subscription`` now
#   opens a Dodo CHECKOUT SESSION (``checkout_sessions.create``) instead of
#   ``subscriptions.create(payment_link=True)``. The old call accepted no
#   return_url, stranding the buyer after payment; the session carries return_url +
#   cancel_url (threaded in from the caller's Origin) so the buyer is returned to
#   the app. The recurring product rides as a ``product_cart`` line and the
#   subscription is created at payment; the response ``session_id`` is carried on
#   ``SubscriptionCheckout.subscription_id``. The webhook grant is untouched (it
#   reads the subscription_id + metadata off the event body, not this response).

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw_ee.cloud._core.errors import BadRequest, ValidationError
from pocketpaw_ee.cloud.billing.domain import (
    GatewayEvent,
    OneTimeCheckout,
    SubscriptionCheckout,
    SubscriptionEvent,
)

logger = logging.getLogger(__name__)

# The verified subscription event families this provider normalizes. Other
# subscription.* deliveries (on_hold / paused / failed / expired / plan_changed /
# updated) are still parsed into a SubscriptionEvent — the service decides which
# ones act — but these three are the ones BC-7's grant/revert logic keys on.
_SUBSCRIPTION_EVENT_PREFIX = "subscription."

# Default billing country for the hosted checkout. Dodo requires a country on
# the billing address; the buyer can change it on the hosted page. "US" is a
# neutral default — the real value is confirmed by the customer at checkout.
_DEFAULT_BILLING_COUNTRY = "US"
# A new-customer line needs an email. Dodo collects the real one on the hosted
# page; this placeholder only satisfies the create call when the caller has no
# email on file. The buyer's actual email lands on the resulting Payment.
_PLACEHOLDER_EMAIL = "billing@pocketpaw.local"


def _customer_param(email: str | None) -> dict[str, str]:
    """Build Dodo's ``CustomerRequest`` (new-customer variant).

    Dodo's ``customer`` is an untagged enum: ``{customer_id}`` (attach existing) OR
    ``{email, name}`` (new customer). Sending ``{email}`` alone fails the server-side
    variant match with a 422 (``did not match any variant of untagged enum
    CustomerRequest``) — a NAME is required too. Derive a display name from the email
    local-part when the caller has none; Dodo collects the real details on the hosted
    page either way.
    """
    e = email or _PLACEHOLDER_EMAIL
    return {"email": e, "name": e.split("@", 1)[0] or "Customer"}


class DodoProvider:
    """Dodo Payments gateway adapter. Implements ``IPaymentsProvider``."""

    def __init__(
        self,
        *,
        api_key: str | None,
        environment: str,
        webhook_secret: str | None,
        credit_product_id: str | None,
        plan_products: dict[str, str] | None = None,
        billing_country: str = _DEFAULT_BILLING_COUNTRY,
    ) -> None:
        # Stored, not validated here — a deployment may construct the provider
        # with billing disabled. Each call validates the inputs IT needs, so a
        # misconfiguration fails loudly at the point of use with a clear code.
        self._api_key = api_key
        self._environment = environment or "test_mode"
        self._webhook_secret = webhook_secret
        self._credit_product_id = credit_product_id
        # plan_key -> recurring product_id (BC-7). Used only for the REVERSE map
        # at webhook-parse time (product_id -> plan_key) when a subscription
        # delivery's metadata lacks plan_key; the forward direction (picking a
        # product for a tier) is the service's job, passed in to create_subscription.
        self._plan_products = dict(plan_products or {})
        # ISO-3166 alpha-2 country prefilled on the hosted checkout's billing
        # address — drives which payment methods Dodo surfaces (e.g. IN -> UPI).
        self._billing_country = billing_country or _DEFAULT_BILLING_COUNTRY

    @classmethod
    def from_settings(cls, settings: Any) -> DodoProvider:
        """Build a provider from a ``pocketpaw.config.Settings`` instance."""
        plan_products = getattr(settings, "dodo_plan_products", None)
        return cls(
            api_key=getattr(settings, "dodo_payments_api_key", None),
            environment=getattr(settings, "dodo_environment", "test_mode"),
            webhook_secret=getattr(settings, "dodo_webhook_secret", None),
            credit_product_id=getattr(settings, "dodo_credit_product_id", None),
            plan_products=plan_products if isinstance(plan_products, dict) else None,
            billing_country=getattr(settings, "dodo_billing_country", _DEFAULT_BILLING_COUNTRY),
        )

    def _plan_key_for_product(self, product_id: str) -> str:
        """Reverse-resolve a tier key from a recurring product id, or ''.

        The plan->product config is the forward map; this inverts it so a
        subscription webhook whose metadata is missing ``plan_key`` can still be
        routed by the product id Dodo always sends on ``data.product_id``.
        """
        if not product_id:
            return ""
        for key, pid in self._plan_products.items():
            if pid == product_id:
                return str(key)
        return ""

    # ------------------------------------------------------------------ #
    # Checkout
    # ------------------------------------------------------------------ #

    def _client(self):  # pragma: no cover - thin SDK wiring, mocked in tests
        """Construct the async DodoPayments client from settings.

        Imported lazily so importing this module (e.g. at app mount) never pulls
        the SDK, and so tests can patch ``billing.providers.dodo.AsyncDodoPayments``.
        """
        if not self._api_key:
            raise ValidationError(
                "billing.gateway_unconfigured",
                "Dodo Payments API key is not configured (POCKETPAW_DODO_PAYMENTS_API_KEY).",
            )
        return AsyncDodoPayments(bearer_token=self._api_key, environment=self._environment)

    async def create_one_time(
        self,
        *,
        amount_credits: int,
        workspace_id: str,
        customer_email: str | None,
        metadata: dict,
    ) -> OneTimeCheckout:
        if (
            not isinstance(amount_credits, int)
            or isinstance(amount_credits, bool)
            or amount_credits <= 0
        ):
            raise ValidationError(
                "billing.invalid_amount", "amount_credits must be a positive integer"
            )
        if not workspace_id:
            raise ValidationError("billing.invalid_workspace", "workspace_id is required")
        if not self._credit_product_id:
            raise ValidationError(
                "billing.product_unconfigured",
                "Dodo credit product id is not configured (POCKETPAW_DODO_CREDIT_PRODUCT_ID).",
            )

        # 1 credit == $0.01 == 1 cent == the currency's lowest denomination, so
        # amount_credits maps 1:1 onto Dodo's amount field — no division.
        cart_amount_cents = amount_credits

        # workspace_id MUST ride on metadata so the webhook can route the grant.
        # The provider stamps it authoritatively, overriding any caller value.
        meta = {k: str(v) for k, v in dict(metadata or {}).items()}
        meta["workspace_id"] = str(workspace_id)

        client = self._client()
        response = await client.payments.create(
            billing={"country": self._billing_country},
            customer=_customer_param(customer_email),
            product_cart=[
                {
                    "product_id": self._credit_product_id,
                    "quantity": 1,
                    "amount": cart_amount_cents,
                }
            ],
            payment_link=True,
            metadata=meta,
        )

        checkout_url = getattr(response, "payment_link", None)
        gateway_ref = getattr(response, "payment_id", "") or ""
        if not checkout_url:
            # payment_link is only populated when payment_link=True succeeds.
            raise ValidationError(
                "billing.no_checkout_url",
                "Dodo did not return a hosted payment link for the top-up.",
            )
        return OneTimeCheckout(checkout_url=str(checkout_url), gateway_ref=str(gateway_ref))

    # ------------------------------------------------------------------ #
    # Subscriptions (BC-7)
    # ------------------------------------------------------------------ #

    async def create_subscription(
        self,
        *,
        plan_key: str,
        product_id: str,
        workspace_id: str,
        customer_email: str | None,
        metadata: dict,
        return_url: str | None = None,
        cancel_url: str | None = None,
    ) -> SubscriptionCheckout:
        if not plan_key:
            raise ValidationError("billing.invalid_plan", "plan_key is required")
        if not workspace_id:
            raise ValidationError("billing.invalid_workspace", "workspace_id is required")
        if not product_id:
            raise ValidationError(
                "billing.plan_product_unconfigured",
                f"No Dodo recurring product id is configured for plan '{plan_key}' "
                "(POCKETPAW_DODO_PLAN_PRODUCTS).",
            )

        # workspace_id AND plan_key MUST ride on metadata so the renewal webhook
        # can route the grant to the right wallet at the right tier. The provider
        # stamps both authoritatively, overriding any caller value.
        meta = {k: str(v) for k, v in dict(metadata or {}).items()}
        meta["workspace_id"] = str(workspace_id)
        meta["plan_key"] = str(plan_key)

        # CHECKOUT SESSIONS (the fix): ``subscriptions.create(payment_link=True)``
        # accepts NO return_url, so the buyer was stranded after paying on Dodo.
        # The Checkout Sessions API DOES carry return_url + cancel_url. A recurring
        # product passed as a ``product_cart`` line creates the SUBSCRIPTION at
        # payment time; the response is a SESSION (checkout_url + session_id), and
        # the real subscription_id arrives later on the verified subscription.active
        # webhook body — NOT from this create call.
        #
        # ``billing_address.country`` (was ``billing={"country": ...}`` on the old
        # subscriptions API) preserves the configurable billing country so IN+INR
        # products still surface UPI; the buyer can change it on the hosted page.
        create_kwargs: dict[str, Any] = {
            "product_cart": [{"product_id": product_id, "quantity": 1}],
            "customer": _customer_param(customer_email),
            "billing_address": {"country": self._billing_country},
            "metadata": meta,
        }
        # Only attach return_url/cancel_url when the caller supplied a base — Dodo
        # treats a missing return_url as "no redirect"; sending an empty string is
        # worse than omitting it. The sites publish path passes neither.
        if return_url:
            create_kwargs["return_url"] = return_url
        if cancel_url:
            create_kwargs["cancel_url"] = cancel_url

        client = self._client()
        response = await client.checkout_sessions.create(**create_kwargs)

        checkout_url = getattr(response, "checkout_url", None)
        # The session id is repurposed onto SubscriptionCheckout.subscription_id —
        # the create response has no subscription yet (it's created at payment). The
        # authoritative gateway subscription_id comes in on the webhook body.
        session_id = getattr(response, "session_id", "") or ""
        if not checkout_url:
            # checkout_url is None only when payment_method_id was passed (we never
            # do) or the create failed — treat a missing url as a hard error.
            raise ValidationError(
                "billing.no_checkout_url",
                "Dodo did not return a checkout session url for the subscription.",
            )
        return SubscriptionCheckout(
            checkout_url=str(checkout_url), subscription_id=str(session_id)
        )

    async def cancel_subscription(self, subscription_id: str) -> None:
        if not subscription_id:
            raise ValidationError("billing.invalid_subscription", "subscription_id is required")
        # The SDK has no ``.cancel`` — cancellation is a status PATCH via
        # ``subscriptions.update(<id>, status="cancelled")``. Dodo emits the
        # ``subscription.cancelled`` webhook that drives the entitlement revert.
        client = self._client()
        await client.subscriptions.update(subscription_id, status="cancelled")

    # ------------------------------------------------------------------ #
    # Webhook
    # ------------------------------------------------------------------ #

    def verify_and_parse_webhook(
        self,
        *,
        payload: bytes,
        headers: dict[str, str],
    ) -> GatewayEvent | SubscriptionEvent:
        if not self._webhook_secret:
            raise ValidationError(
                "billing.webhook_unconfigured",
                "Dodo webhook secret is not configured (POCKETPAW_DODO_WEBHOOK_SECRET).",
            )

        # SECURITY: verify the signature against the RAW bytes BEFORE the body is
        # parsed or trusted. ``Webhook`` takes ``whsecret`` (strips a ``whsec_``
        # prefix, base64-decodes); ``.verify`` returns the parsed JSON and RAISES
        # ``WebhookVerificationError`` on a bad/missing signature, malformed
        # headers, or a timestamp outside the 5-minute tolerance.
        wh = Webhook(self._webhook_secret)
        try:
            verified = wh.verify(payload, headers)
        except WebhookVerificationError as exc:
            # Do NOT include the secret or signature in the message. A bad/missing
            # signature is a 400 (untrusted request), not a 422 (field rule).
            raise BadRequest(
                "billing.webhook_invalid_signature",
                "Dodo webhook signature did not verify.",
            ) from exc

        # The Standard-Webhooks delivery id is the idempotency key. It lives in
        # the ``webhook-id`` header (verify already required it to be present).
        lower = {k.lower(): v for k, v in headers.items()}
        event_id = lower.get("webhook-id", "")
        if not event_id:  # pragma: no cover - verify() already enforces this
            raise BadRequest(
                "billing.webhook_missing_id", "Dodo webhook is missing the webhook-id header."
            )

        if not isinstance(verified, dict):
            # verify() returns json.loads(...) — normally a dict; be defensive.
            try:
                verified = json.loads(payload or b"{}")
            except ValueError as exc:
                raise BadRequest(
                    "billing.webhook_malformed", "Dodo webhook body was not valid JSON."
                ) from exc

        event_type = str(verified.get("type") or "")
        data = verified.get("data") if isinstance(verified.get("data"), dict) else {}

        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        workspace_id = str(meta.get("workspace_id") or "")

        # SUBSCRIPTION family (BC-7) — a ``subscription.*`` delivery normalizes to
        # a ``SubscriptionEvent``, NOT a GatewayEvent. The tier comes from the
        # metadata's ``plan_key`` (stamped at subscribe time); if it's missing,
        # fall back to reverse-mapping the recurring ``data.product_id`` through
        # the configured plan->product mapping. No money amount on the wire here —
        # the grant uses the catalog's allotment, not an event amount.
        if event_type.startswith(_SUBSCRIPTION_EVENT_PREFIX):
            product_id = str(data.get("product_id") or "")
            plan_key = str(meta.get("plan_key") or "") or self._plan_key_for_product(product_id)
            subscription_id = str(data.get("subscription_id") or "")
            # BC-9: a per-site annual sub stamps ``site_id`` on its metadata; a
            # workspace-plan sub does not. This is the discriminator the service
            # routes on — a delivery WITH a site_id updates the SITE, one WITHOUT
            # it follows the BC-7 workspace path. Pull it straight off metadata.
            site_id = str(meta.get("site_id") or "")
            return SubscriptionEvent(
                event_id=event_id,
                type=event_type,
                workspace_id=workspace_id,
                plan_key=plan_key,
                product_id=product_id,
                subscription_id=subscription_id,
                site_id=site_id,
                raw=verified,
            )

        # ``total_amount`` is the lowest denomination (cents for USD). 1 credit ==
        # 1 cent, so cents map 1:1 back onto credits.
        raw_amount = data.get("total_amount")
        if isinstance(raw_amount, int):
            amount_credits = int(raw_amount)
        else:
            # Safe-coerce to 0 (the service's amount<=0 guard then no-ops the
            # grant), but warn so ops can triage a malformed/missing amount on
            # a success event. No money value to leak here — it isn't a number.
            amount_credits = 0
            if event_type == "payment.succeeded":
                logger.warning(
                    "billing.webhook: payment.succeeded had non-int total_amount "
                    "(type=%s, event_id=%s) — coercing to 0, grant will no-op",
                    type(raw_amount).__name__,
                    event_id,
                )
        currency = str(data.get("currency") or "")

        return GatewayEvent(
            event_id=event_id,
            type=event_type,
            amount_credits=amount_credits,
            workspace_id=workspace_id,
            currency=currency,
            raw=verified,
        )


# Lazy SDK imports — pulled at module import time so they CAN be monkeypatched on
# this module in tests, but the actual network client is only built inside
# ``_client()``. ``standardwebhooks`` is pure-Python (no network), safe to import.
from dodopayments import AsyncDodoPayments  # noqa: E402
from standardwebhooks import Webhook, WebhookVerificationError  # noqa: E402
