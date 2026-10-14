# ee/pocketpaw_ee/integrations/providers/razorpay.py: the ``razorpay`` payments provider.
#
# Razorpay Technology Partner OAuth (authorization code, no PKCE: Razorpay's
# authorize endpoint takes none, so the flow rests on the cookie-bound single-use
# state). App config: ``POCKETPAW_INTEGRATIONS_RAZORPAY_CLIENT_ID`` / ``_SECRET``;
# without both it is ``unconfigured``. ``_MODE=test`` sends ``mode: test`` on the
# code exchange (a development client); in production (``POCKETPAW_ENV`` / secure
# cookies, see ``cloud.auth.core._is_production``) test mode makes the provider
# ``unconfigured`` with a warning, so live sellers never get test-mode links.
# Scope ``read_write``. The token response names the merchant
# (``razorpay_account_id``) = ``account_ref``. Access tokens live ~90 days
# (``expires_in`` is read), refresh tokens 180 days and ROTATE: each
# refresh returns a new one and kills the old, which the vault persists under its
# lease. A refresh Razorpay refuses as revoked / undecryptable / not linked raises
# InvalidGrant.
#
# ``create_checkout {product_ref, qty}`` prices server-side from the site's Paw Bar
# catalog (never from the caller), in ISO 4217 minor units, in the binding's
# currency (INR default; a product in another currency is refused). It creates a
# Payment Link on the merchant account with the access token as Bearer:
# ``reference_id`` = a fresh ``paw_`` order id, ``notes`` {site_id, pocket_id,
# product_ref, ...}, ``callback_url`` = the site's own origin + ``success_path``
# (GET). Links expire after ``LINK_TTL_SECONDS``. Returns ``{url: short_url}``.
# Payment Links have no cancel redirect, so there is no ``cancel_path`` setting.
#
# Webhooks: one application webhook for every merchant, signed with
# ``POCKETPAW_INTEGRATIONS_RAZORPAY_WEBHOOK_SECRET`` (``X-Razorpay-Signature`` =
# hex HMAC-SHA256 of the raw body), checked before anything is parsed. An event
# whose signed ``created_at`` is over ``MAX_EVENT_AGE_SECONDS`` old is refused
# (Razorpay retries for 24 h). The dedupe id is the unsigned ``X-Razorpay-Event-Id``
# header bound to the body's sha256, so a forged header can't shadow a real event.
# Orders are keyed by the Razorpay order id (``order_...``) the link created, which
# payments and refunds also carry:
# ``payment_link.paid`` -> order.paid (only for links we made: notes.site_id),
# a full ``refund.processed`` -> order.refunded, ``payment.failed`` -> order.failed;
# the last two only touch an order that already exists (an early refund is
# answered 500 by webhooks.py, so Razorpay redelivers it). ``account_ref`` =
# ``account_id``, so webhooks.py refuses an event whose account isn't the site's.

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

from pocketpaw.money import exponent, normalize_currency
from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.integrations.providers._commerce import (
    MAX_PATH,
    CheckoutArgs,
    catalog_product,
    site_origin,
)
from pocketpaw_ee.integrations.providers._commerce import safe_path as _safe_path
from pocketpaw_ee.integrations.providers.base import (
    ActionContext,
    ActionRefused,
    ActionSpec,
    Availability,
    BaseProvider,
    InvalidGrant,
    NormalizedEvent,
    ProviderError,
    SettingOption,
    SettingsField,
    TokenGrant,
)

logger = logging.getLogger(__name__)

ENV_PREFIX = "POCKETPAW_INTEGRATIONS_RAZORPAY"
WEBHOOK_SECRET_ENV = f"{ENV_PREFIX}_WEBHOOK_SECRET"
MODE_ENV = f"{ENV_PREFIX}_MODE"
AUTHORIZE_URL = "https://auth.razorpay.com/authorize"
TOKEN_URL = "https://auth.razorpay.com/token"
PAYMENT_LINKS_URL = "https://api.razorpay.com/v1/payment_links"
SCOPE = "read_write"
DEFAULT_CURRENCY = "INR"
# Razorpay's minimum charge for the 2-decimal currencies we offer (INR: 100 paise).
MIN_AMOUNT_MINOR = 100
MAX_AMOUNT_MINOR = 99_999_999
# Payment Links must live >= 15 min; a checkout link that sits unpaid for a day is dead.
LINK_TTL_SECONDS = 24 * 3600
# Razorpay retries a failed delivery for 24 h; anything older is a replay.
MAX_EVENT_AGE_SECONDS = 24 * 3600
_NOTE_MAX = 255
# Razorpay's refresh-token refusals (its error page); any of them means reconnect.
_DEAD_REFRESH = ("refresh token", "revoked", "not linked to client", "invalid_grant")

# Razorpay-supported currencies whose exponent matches the money helpers (checked
# at import, so a drift fails loudly). 3-decimal ones are left out: Razorpay wants
# their last digit 0, which a catalog price can't promise.
_EXPONENTS = {
    "INR": 2, "USD": 2, "EUR": 2, "GBP": 2, "AED": 2, "AUD": 2,
    "CAD": 2, "SGD": 2, "CHF": 2, "NZD": 2, "HKD": 2, "JPY": 0,
}  # fmt: skip
CURRENCIES = tuple(normalize_currency(c) for c in _EXPONENTS)
assert all(exponent(c) == e for c, e in _EXPONENTS.items()), "razorpay currency exponent drift"


def _is_test_mode() -> bool:
    return os.environ.get(MODE_ENV, "").strip().lower() == "test"


def _test_mode_in_production() -> bool:
    from pocketpaw_ee.cloud.auth.core import _is_production

    if _is_test_mode() and _is_production():
        logger.warning("razorpay: %s=test in production; provider disabled", MODE_ENV)
        return True
    return False


def webhook_secret() -> str:
    return os.environ.get(WEBHOOK_SECRET_ENV, "").strip()


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ oauth


async def _token_call(payload: dict[str, str], *, refreshing: bool) -> dict[str, Any]:
    """POST JSON to Razorpay's token endpoint (its documented body format)."""
    try:
        async with oauth_http.client() as http:
            resp = await http.post(TOKEN_URL, json=payload, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"razorpay: token endpoint unreachable ({type(exc).__name__})"
        ) from None
    body = oauth_http.body_of(resp)
    if resp.status_code >= 400:
        err = body.get("error")
        parts = (
            [str(err.get("code") or ""), str(err.get("description") or "")]
            if isinstance(err, dict)
            else [str(err or "")]
        )
        parts.append(str(body.get("error_description") or body.get("message") or ""))
        reason = " ".join(parts).lower()
        if refreshing and any(marker in reason for marker in _DEAD_REFRESH):
            raise InvalidGrant("razorpay: refresh token refused")
        raise ProviderError(f"razorpay: token endpoint answered {resp.status_code}")
    if not body.get("access_token"):
        raise ProviderError("razorpay: token response had no access token")
    return body


# --------------------------------------------------------------- checkout


def _note(value: Any) -> str:
    return str(value)[:_NOTE_MAX]


async def _create_checkout(ctx: ActionContext, args: CheckoutArgs) -> dict[str, Any]:
    product = await catalog_product(ctx.workspace_id, ctx.pocket_id, args.product_ref)
    if product is None or product.in_stock is False or product.price_cents <= 0:
        raise ActionRefused("invalid_args", "That product isn't available.")
    try:
        currency = normalize_currency(product.currency)
    except ValueError:
        raise ActionRefused("invalid_args", "That product has no valid currency.") from None
    wanted = str(ctx.settings.get("currency") or DEFAULT_CURRENCY).upper()
    if currency != wanted or currency not in CURRENCIES:
        raise ActionRefused("invalid_args", "That product is priced in another currency.")
    total = product.price_cents * args.qty
    if total > MAX_AMOUNT_MINOR:
        raise ActionRefused("invalid_args", "That order is too large to check out.")
    if exponent(currency) == 2 and total < MIN_AMOUNT_MINOR:
        raise ActionRefused("invalid_args", "That order is below Razorpay's minimum.")

    origin = await site_origin(ctx.site_id)
    if not origin:
        raise ProviderError("site has no public address yet")
    name = (product.name or product.id)[:200]
    payload = {
        "amount": total,
        "currency": currency,
        "accept_partial": False,
        "reference_id": f"paw_{secrets.token_hex(12)}",
        "description": f"{name} x {args.qty}" if args.qty > 1 else name,
        "expire_by": int(time.time()) + LINK_TTL_SECONDS,
        "notes": {
            "site_id": _note(ctx.site_id),
            "pocket_id": _note(ctx.pocket_id),
            "product_ref": _note(product.id),
            "product_name": _note(name),
            "qty": str(args.qty),
            "unit_amount": str(product.price_cents),
        },
        "callback_url": origin + _safe_path(ctx.settings.get("success_path")),
        "callback_method": "get",
    }
    resp = await oauth_http.authed(
        ctx.get_token, "POST", PAYMENT_LINKS_URL, provider="razorpay", json=payload
    )
    if resp.status_code == 429:
        raise ActionRefused("rate_limited")
    if resp.status_code == 400:
        raise ActionRefused("invalid_args", "Razorpay didn't accept that checkout.")
    if resp.status_code >= 400:
        raise ProviderError(f"razorpay: payment link answered {resp.status_code}")
    url = str(oauth_http.body_of(resp).get("short_url") or "")
    if not url.startswith("https://"):
        raise ProviderError("razorpay returned no payment link")
    return {"url": url}


# --------------------------------------------------------------- webhooks


def sign(body: bytes, secret: str) -> str:
    """The ``X-Razorpay-Signature`` value for ``body`` (tests and local tooling)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(body, secret), signature.strip())


def _entity(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    wrapper = payload.get(name)
    entity = wrapper.get("entity") if isinstance(wrapper, Mapping) else None
    return entity if isinstance(entity, Mapping) else {}


def _notes(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    # Razorpay sends empty notes as ``[]``.
    notes = entity.get("notes")
    return notes if isinstance(notes, Mapping) else {}


def _paid_order(link: Mapping[str, Any], payment: Mapping[str, Any]) -> dict[str, Any]:
    notes = _notes(link) or _notes(payment)
    customer = link.get("customer") if isinstance(link.get("customer"), Mapping) else {}
    email = customer.get("email") or payment.get("email")
    amount = _int(link.get("amount_paid")) or _int(payment.get("amount")) or 0
    qty = max(1, _int(notes.get("qty")) or 1)
    return {
        "order_id": str(payment.get("order_id") or link.get("order_id") or ""),
        "amount_minor": amount,
        "currency": str(link.get("currency") or payment.get("currency") or "").upper(),
        "customer_email": email if isinstance(email, str) and email else None,
        "items": [
            {
                "ref": str(notes.get("product_ref") or ""),
                "name": str(notes.get("product_name") or ""),
                "qty": qty,
                "amount_minor": amount,
            }
        ],
    }


def _fully_refunded(payment: Mapping[str, Any]) -> bool:
    """Only a full refund settles an order; a partial one leaves it paid."""
    if payment.get("refund_status") == "full":
        return True
    amount, refunded = _int(payment.get("amount")), _int(payment.get("amount_refunded"))
    return bool(amount and refunded is not None and amount > 0 and refunded >= amount)


def normalize_event(event: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
    """(our type, data, site_id) for a verified Razorpay event."""
    kind = str(event.get("event") or "")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return "ignored", {}, ""
    payment = _entity(payload, "payment")
    order_id = str(payment.get("order_id") or "")
    site_id = str(_notes(payment).get("site_id") or "")
    if kind == "payment_link.paid":
        link = _entity(payload, "payment_link")
        data = _paid_order(link, payment)
        site_id = str(_notes(link).get("site_id") or site_id)
        # Only links this platform made carry our site id; the merchant's own
        # links are none of a site's business.
        if not site_id or not data["order_id"]:
            return "ignored", {}, ""
        return "order.paid", data, site_id
    if kind == "refund.processed" and order_id and _fully_refunded(payment):
        return "order.refunded", {"order_id": order_id, "only_if_exists": True}, site_id
    if kind == "payment.failed" and order_id:
        return "order.failed", {"order_id": order_id, "only_if_exists": True}, site_id
    return "ignored", {}, ""


# --------------------------------------------------------------- provider


class RazorpayProvider(BaseProvider):
    id = "razorpay"
    name = "Razorpay"
    kind = "connect"
    category = "payments"
    plan = "site"
    supports_pkce = False
    actions = {
        "create_checkout": ActionSpec(
            args=CheckoutArgs,
            handler=_create_checkout,
            description="Start a Razorpay payment for one catalog product; returns its url.",
            public=True,
            write=True,
        ),
    }
    settings_fields = (
        SettingsField(
            key="currency",
            label="Currency",
            type="select",
            help="Checkout only sells products priced in this currency. Default: INR.",
            options=tuple(SettingOption(c, c) for c in CURRENCIES),
        ),
        SettingsField(
            key="success_path",
            label="After payment, send buyers to",
            help="A path on your site, like /thanks. Defaults to your home page.",
            max_length=MAX_PATH,
        ),
    )

    def availability(self) -> Availability:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        if not client_id or not secret or _test_mode_in_production():
            return "unconfigured"
        return "available"

    def _credentials(self) -> tuple[str, str]:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        if not client_id or not secret or _test_mode_in_production():
            raise ProviderError("razorpay: not configured")
        return client_id, secret

    def authorize_url(
        self,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,  # noqa: ARG002 - Razorpay has no PKCE
    ) -> str:
        client_id, _ = self._credentials()
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def complete(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,  # noqa: ARG002 - Razorpay has no PKCE
    ) -> TokenGrant:
        client_id, secret = self._credentials()
        payload = {
            "client_id": client_id,
            "client_secret": secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        }
        if _is_test_mode():
            payload["mode"] = "test"
        body = await _token_call(payload, refreshing=False)
        account_id = str(body.get("razorpay_account_id") or "")
        if not account_id:
            raise ProviderError("razorpay: no account id in the grant")
        return TokenGrant(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=[SCOPE],
            account_label=f"Razorpay {account_id}",
            account_ref=account_id,
        )

    async def refresh(self, refresh_token: str) -> TokenGrant:
        client_id, secret = self._credentials()
        body = await _token_call(
            {
                "client_id": client_id,
                "client_secret": secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            refreshing=True,
        )
        return TokenGrant(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
        )

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> NormalizedEvent | None:
        if not verify_signature(body, headers.get("x-razorpay-signature", ""), webhook_secret()):
            return None
        try:
            event = json.loads(body)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        created = event.get("created_at")
        if (
            not isinstance(created, int)
            or isinstance(created, bool)
            or time.time() - created > MAX_EVENT_AGE_SECONDS
        ):
            return None
        # The header is unsigned: bound to the signed body so a redelivery (same
        # header, same bytes) dedupes and nothing else can collide with it.
        header_id = headers.get("x-razorpay-event-id", "").strip()[:100]
        kind, data, site_id = normalize_event(event)
        return NormalizedEvent(
            id=f"{header_id}:{hashlib.sha256(body).hexdigest()}",
            type=kind,
            provider=self.id,
            data=data,
            site_id=site_id,
            account_ref=str(event.get("account_id") or ""),
        )
