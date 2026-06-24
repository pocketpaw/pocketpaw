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
#   * The hosted url comes back as ``response.payment_link`` (only populated when
#     ``payment_link=True``); ``response.payment_id`` is the gateway ref.
#   * The webhook body is ``{business_id, data: Payment, timestamp, type}``; the
#     buyer's metadata lives at ``data.metadata`` and the money amount at
#     ``data.total_amount`` (lowest denomination) with ``data.currency``.
#
# SECURITY: the signature is verified before the payload is parsed. The webhook
# secret and the API key are NEVER logged.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new module.
# Updated 2026-06-24 (security): warn (don't silently swallow) when a
#   payment.succeeded event carries a non-int / missing ``total_amount`` — the
#   safe-coerce-to-0 behavior is unchanged, but ops now get a log to triage.

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw_ee.cloud._core.errors import BadRequest, ValidationError
from pocketpaw_ee.cloud.billing.domain import GatewayEvent, OneTimeCheckout

logger = logging.getLogger(__name__)

# Default billing country for the hosted checkout. Dodo requires a country on
# the billing address; the buyer can change it on the hosted page. "US" is a
# neutral default — the real value is confirmed by the customer at checkout.
_DEFAULT_BILLING_COUNTRY = "US"
# A new-customer line needs an email. Dodo collects the real one on the hosted
# page; this placeholder only satisfies the create call when the caller has no
# email on file. The buyer's actual email lands on the resulting Payment.
_PLACEHOLDER_EMAIL = "billing@pocketpaw.local"


class DodoProvider:
    """Dodo Payments gateway adapter. Implements ``IPaymentsProvider``."""

    def __init__(
        self,
        *,
        api_key: str | None,
        environment: str,
        webhook_secret: str | None,
        credit_product_id: str | None,
    ) -> None:
        # Stored, not validated here — a deployment may construct the provider
        # with billing disabled. Each call validates the inputs IT needs, so a
        # misconfiguration fails loudly at the point of use with a clear code.
        self._api_key = api_key
        self._environment = environment or "test_mode"
        self._webhook_secret = webhook_secret
        self._credit_product_id = credit_product_id

    @classmethod
    def from_settings(cls, settings: Any) -> DodoProvider:
        """Build a provider from a ``pocketpaw.config.Settings`` instance."""
        return cls(
            api_key=getattr(settings, "dodo_payments_api_key", None),
            environment=getattr(settings, "dodo_environment", "test_mode"),
            webhook_secret=getattr(settings, "dodo_webhook_secret", None),
            credit_product_id=getattr(settings, "dodo_credit_product_id", None),
        )

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
            billing={"country": _DEFAULT_BILLING_COUNTRY},
            customer={"email": customer_email or _PLACEHOLDER_EMAIL},
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
    # Webhook
    # ------------------------------------------------------------------ #

    def verify_and_parse_webhook(
        self,
        *,
        payload: bytes,
        headers: dict[str, str],
    ) -> GatewayEvent:
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
