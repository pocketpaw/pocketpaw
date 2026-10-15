# ee/pocketpaw_ee/integrations/providers/stripe.py: the ``stripe`` payments provider.
#
# Stripe Connect with Accounts v2 and Account Links, not OAuth. The platform key
# (``POCKETPAW_STRIPE_SECRET_KEY``) creates one connected account per owner with
# ``POST /v2/core/accounts`` (merchant configuration requesting card_payments, full
# Stripe dashboard, fees_collector + losses_collector ``stripe``, and the required
# ``identity.country``: the workspace's partner country, else
# ``POCKETPAW_STRIPE_DEFAULT_COUNTRY``, default US; fixed once created) and a v2
# ``account_onboarding`` link back to our callback with ``code=<acct id>``. New
# platforms can't create v1 accounts, so nothing here calls ``POST /v1/accounts``.
# /v2 calls send JSON and ``V2_STRIPE_VERSION``; /v1 calls stay form-encoded on
# ``STRIPE_VERSION``. No token is stored: the grant's ``access_token`` is the
# account id (``acct_``), the one value checkout needs (``Stripe-Account``).
#
# BINDING. The account id is bound server-side to the flow: authorize_url /
# resume_url store ``acct`` under the flow's PKCE challenge (the verifier lives in
# the single-use state payload), and ``complete`` refuses unless the returned code
# equals the bound id. The query-string ``code`` is never trusted alone. An owner
# who restarts connect, or whose callback came back after the 2 h flow expired,
# gets their pending account back (keyed per workspace + user; reused whatever its
# onboarding state while no connection holds it). Accounts carry metadata
# {workspace_id, user_id}, checked on reuse and in ``complete``.
#
# STATUS. Connection meta is ``charges_enabled`` / ``details_submitted`` /
# ``requirements_due_count``, read from the v2 account (``_v2_meta``: card_payments
# capability ``active``; user-actionable requirements currently or past due) or a
# v1 ``account.updated`` snapshot (``_meta``), which Stripe still sends for v2
# accounts on the Connect (connected accounts) endpoint. Thin v2 account events
# (``v2.core.account[...]``, platform scope, optional second signing secret
# ``POCKETPAW_STRIPE_ACCOUNTS_WEBHOOK_SECRET``) refetch the account in
# ``expand_event``. Either way the webhook layer's ``as_of`` guard drops older state.
#
# ``create_checkout {product_ref, qty}`` prices server-side from the site's Paw
# Bar catalog (``PawBarStore`` rows of the site pocket's widgets), in ISO 4217 minor
# units; success/cancel URLs are the site's own origin + the owner's paths. The
# platform fee is ``PLATFORM_FEE_BPS`` (0: off).
#
# Webhooks: the ``Stripe-Signature`` header is checked (HMAC-SHA256, 5 min
# tolerance) before anything is read. Orders are keyed by PaymentIntent id so
# refunds and failures land on the order checkout created; a refund never creates
# one (one that arrives first is redelivered until it can apply). A platform with
# no Connect signup is logged as a WARNING naming the misconfiguration.

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

from pocketpaw.money import normalize_currency
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
    NormalizedEvent,
    ProviderError,
    SettingOption,
    SettingsField,
    TokenGrant,
)

logger = logging.getLogger(__name__)

SECRET_KEY_ENV = "POCKETPAW_STRIPE_SECRET_KEY"
WEBHOOK_SECRET_ENV = "POCKETPAW_STRIPE_CONNECT_WEBHOOK_SECRET"
# Optional: the signing secret of a thin-event destination ("Your account" scope)
# carrying ``v2.core.account[...]`` events to the same URL.
ACCOUNTS_WEBHOOK_SECRET_ENV = "POCKETPAW_STRIPE_ACCOUNTS_WEBHOOK_SECRET"
# ISO 3166-1 alpha-2 country for new accounts when the workspace has none.
DEFAULT_COUNTRY_ENV = "POCKETPAW_STRIPE_DEFAULT_COUNTRY"
API_BASE = "https://api.stripe.com"
STRIPE_VERSION = "2025-08-27.basil"
V2_STRIPE_VERSION = "2026-09-30.endive"
# What a v2 account read includes; ``metadata`` comes back without asking.
ACCOUNT_INCLUDE = ("configuration.merchant", "requirements", "identity", "defaults")
# Thin events that can move charges / requirements on a merchant account.
ACCOUNT_THIN_EVENTS = frozenset(
    {
        "v2.core.account.updated",
        "v2.core.account[requirements].updated",
        "v2.core.account[configuration.merchant].updated",
        "v2.core.account[configuration.merchant].capability_status_updated",
        "v2.core.account[identity].updated",
    }
)
ACCOUNT_REFRESH = "stripe.account_refresh"
# Stripe's answers when the platform itself never signed up for Connect.
_NO_CONNECT_CODES = frozenset(
    {"platform_registration_required", "non_connect_platform_accounts_v2_access_blocked"}
)
TIMEOUT_SECONDS = 15.0
SIGNATURE_TOLERANCE_SECONDS = 300
# Basis points of each checkout the platform keeps. 0 = no application fee.
PLATFORM_FEE_BPS = 0
# Stripe's per-charge ceiling for most currencies (8 digits of minor units).
MAX_AMOUNT_MINOR = 99_999_999
# Onboarding (identity, bank details) easily outlasts the default 10 minute flow,
# so the flow state, the browser cookie and the account binding all live this long.
FLOW_TTL_SECONDS = 2 * 3600
BIND_TTL_SECONDS = FLOW_TTL_SECONDS
PENDING_TTL_SECONDS = 30 * 24 * 3600
_BIND_KEY = "integrations_stripe_bind:"
_PENDING_KEY = "integrations_stripe_pending:"
_STATE_NAMESPACE = "integrations"

# Checked against the money helpers at import, so a typo fails loudly.
CURRENCIES = tuple(
    normalize_currency(c)
    for c in ("USD", "EUR", "GBP", "INR", "AUD", "CAD", "SGD", "AED", "JPY", "CHF", "NZD")
)


def secret_key() -> str:
    return os.environ.get(SECRET_KEY_ENV, "").strip()


def webhook_secret() -> str:
    return os.environ.get(WEBHOOK_SECRET_ENV, "").strip()


def accounts_webhook_secret() -> str:
    return os.environ.get(ACCOUNTS_WEBHOOK_SECRET_ENV, "").strip()


def _client() -> httpx.AsyncClient:
    """One short-lived client per call. Tests patch this with a MockTransport."""
    return httpx.AsyncClient(base_url=API_BASE, timeout=TIMEOUT_SECONDS)


def _flatten(data: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: ``a[b][0][c]=v``. None is dropped, bools are words."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, Mapping):
            out.extend(_flatten(value, name))
        elif isinstance(value, list | tuple):
            for i, item in enumerate(value):
                if isinstance(item, Mapping):
                    out.extend(_flatten(item, f"{name}[{i}]"))
                else:
                    out.append((f"{name}[{i}]", str(item)))
        elif isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        else:
            out.append((name, str(value)))
    return out


async def _stripe(
    method: str,
    path: str,
    data: Mapping[str, Any] | None = None,
    *,
    account: str = "",
    missing_ok: bool = False,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One Stripe call. /v2 paths send JSON on ``V2_STRIPE_VERSION``; /v1 paths
    send Stripe's form encoding on ``STRIPE_VERSION``."""
    key = secret_key()
    if not key:
        raise ProviderError("stripe is not configured")
    v2 = path.startswith("/v2/")
    version = V2_STRIPE_VERSION if v2 else STRIPE_VERSION
    headers = {"Authorization": f"Bearer {key}", "Stripe-Version": version}
    if account:
        headers["Stripe-Account"] = account
    body = None
    if data is not None and v2:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data)
    elif data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(_flatten(data))
    if query:
        path = f"{path}?{urlencode(_flatten(query))}"
    try:
        async with _client() as client:
            resp = await client.request(method, path, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderError(f"stripe unreachable: {type(exc).__name__}") from None
    if resp.status_code == 404 and missing_ok:
        return None
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code >= 400 or not isinstance(payload, dict):
        err = payload.get("error") if isinstance(payload, dict) else None
        err = err if isinstance(err, Mapping) else {}
        code = err.get("code") or err.get("type") or "error"
        if _no_connect(str(code), str(err.get("message") or "")):
            logger.warning(
                "stripe: the platform account behind %s is not signed up for Connect, so it "
                "can't create connected accounts. Enable Connect in the Stripe Dashboard "
                "for this key's account (test and live mode are separate).",
                SECRET_KEY_ENV,
            )
        raise ProviderError(f"stripe {resp.status_code} {str(code)[:60]}")
    return payload


def _no_connect(code: str, message: str) -> bool:
    """Stripe's "this platform never signed up for Connect" answer (v1 has only the
    message, v2 a code). Never logs the message itself."""
    return code in _NO_CONNECT_CODES or "signed up for connect" in message.lower()


# ------------------------------------------------------------- flow binding


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _redis():
    from pocketpaw_ee.cloud._core import redis_client

    return redis_client.get_redis()


async def _flow(state: str) -> dict[str, Any]:
    from pocketpaw_ee.cloud.auth import _oauth_state

    payload = await _oauth_state.peek(_STATE_NAMESPACE, state)
    if not payload or not payload.get("code_verifier"):
        raise ProviderError("stripe connect flow has no state")
    return payload


async def _bind(flow: dict[str, Any], account_id: str, *, owner: str = "") -> None:
    """Bind ``account_id`` (and who must own it) to this flow. ``owner`` is the
    account's creator when it differs from the flow's user (an admin's resume)."""
    challenge = _s256(str(flow["code_verifier"]))
    bound = {
        "account": account_id,
        "workspace_id": str(flow.get("workspace_id") or ""),
        "user_id": owner or str(flow.get("user_id") or ""),
    }
    await _redis().setex(_BIND_KEY + challenge, BIND_TTL_SECONDS, json.dumps(bound))


async def _take_bound(code_verifier: str) -> dict[str, str]:
    key = _BIND_KEY + _s256(code_verifier)
    redis = _redis()
    raw = await redis.get(key)
    await redis.delete(key)
    try:
        bound = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        bound = {}
    return {k: str(v) for k, v in bound.items()} if isinstance(bound, dict) else {}


def _owned_by(account: Mapping[str, Any], workspace_id: str, user_id: str) -> bool:
    """The account was created by this flow's workspace + user (metadata we stamp)."""
    meta = account.get("metadata") or {}
    return (
        bool(workspace_id)
        and bool(user_id)
        and meta.get("workspace_id") == workspace_id
        and meta.get("user_id") == user_id
    )


def _pending_key(workspace_id: str, user_id: str) -> str:
    return f"{_PENDING_KEY}{workspace_id}:{user_id}"


async def _has_connection(account_id: str) -> bool:
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection as Conn

    row = await Conn.find_one(Conn.provider == "stripe", Conn.account_ref == account_id)
    return row is not None


async def _pending_account(workspace_id: str, user_id: str) -> str:
    """The caller's account from an earlier connect that never became a connection
    (abandoned, or onboarded but its callback came back too late), else "". Its
    onboarding state does not matter: a verified account is still theirs."""
    account_id = str(await _redis().get(_pending_key(workspace_id, user_id)) or "")
    if not account_id.startswith("acct_") or await _has_connection(account_id):
        return ""
    account = await _get_account(account_id, missing_ok=True)
    if not account or not _owned_by(account, workspace_id, user_id):
        return ""
    return account_id


async def _get_account(account_id: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    """The v2 account with what status and label need (``ACCOUNT_INCLUDE``)."""
    return await _stripe(
        "GET",
        f"/v2/core/accounts/{account_id}",
        missing_ok=missing_ok,
        query={"include": list(ACCOUNT_INCLUDE)},
    )


def _iso2(value: Any) -> str:
    """``value`` as an ISO 3166-1 alpha-2 code (two uppercase ASCII letters), else ""."""
    code = str(value or "").strip().upper()
    return code if len(code) == 2 and code.isascii() and code.isalpha() else ""


async def _account_country(workspace_id: str) -> str:
    """``identity.country`` for a new account, which Stripe needs before the merchant
    configuration and which can't change afterwards: the workspace's own country
    (today only a partner profile carries one), else ``DEFAULT_COUNTRY_ENV``."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.workspace import Workspace

    try:
        workspace = await Workspace.get(PydanticObjectId(workspace_id))
    except Exception:  # noqa: BLE001 - an unreadable id or lookup falls back to the default
        workspace = None
    partner = getattr(workspace, "partner", None)
    for value in (getattr(partner, "country", None), getattr(partner, "billing_country", None)):
        if code := _iso2(value):
            return code
    raw = os.environ.get(DEFAULT_COUNTRY_ENV, "").strip() or "US"
    code = _iso2(raw)
    if not code:
        logger.warning("stripe: %s is not an ISO 3166-1 alpha-2 code", DEFAULT_COUNTRY_ENV)
        raise ProviderError("stripe default country is invalid")
    return code


async def _create_account(workspace_id: str, user_id: str) -> str:
    """A v2 merchant account: card payments, the full Stripe dashboard, Stripe
    collects its fees from the account and owns negative balances (so Stripe also
    collects the requirements, through its hosted onboarding). ``identity.country``
    is required before the merchant configuration."""
    country = await _account_country(workspace_id)
    account = await _stripe(
        "POST",
        "/v2/core/accounts",
        {
            "dashboard": "full",
            "identity": {"country": country},
            "configuration": {"merchant": {"capabilities": {"card_payments": {"requested": True}}}},
            "defaults": {
                "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"}
            },
            "metadata": {"workspace_id": workspace_id, "user_id": user_id},
        },
    )
    account_id = str((account or {}).get("id") or "")
    if not account_id.startswith("acct_"):
        raise ProviderError("stripe returned no account id")
    return account_id


async def _account_link(account_id: str, state: str, redirect_uri: str) -> str:
    sep = "&" if "?" in redirect_uri else "?"
    link = await _stripe(
        "POST",
        "/v2/core/account_links",
        {
            "account": account_id,
            "use_case": {
                "type": "account_onboarding",
                "account_onboarding": {
                    "return_url": f"{redirect_uri}{sep}"
                    + urlencode({"state": state, "code": account_id}),
                    # An expired link lands on our callback with no code: the popup
                    # reports it and the owner's next connect reuses this account.
                    "refresh_url": f"{redirect_uri}{sep}" + urlencode({"state": state}),
                },
            },
        },
    )
    url = str((link or {}).get("url") or "")
    if not url.startswith("https://"):
        raise ProviderError("stripe returned no onboarding link")
    return url


def _meta(account: Mapping[str, Any]) -> dict[str, Any]:
    """Meta from a v1 account snapshot (the ``account.updated`` event)."""
    reqs = account.get("requirements") or {}
    due = reqs.get("currently_due") if isinstance(reqs, Mapping) else None
    return {
        "charges_enabled": bool(account.get("charges_enabled")),
        "details_submitted": bool(account.get("details_submitted")),
        "requirements_due_count": len(due) if isinstance(due, list) else 0,
    }


def _v2_meta(account: Mapping[str, Any]) -> dict[str, Any]:
    """The same meta keys from a v2 account. charges_enabled: the merchant
    card_payments capability is ``active``. requirements_due_count: entries the
    owner must act on (``awaiting_action_from: user``) that are currently or past
    due, what v1 lists in ``currently_due``. details_submitted: requirements came
    back and none of them waits on the owner (v2 has no such flag)."""
    merchant = (account.get("configuration") or {}).get("merchant") or {}
    card = (merchant.get("capabilities") or {}).get("card_payments") or {}
    reqs = account.get("requirements")
    entries = reqs.get("entries") if isinstance(reqs, Mapping) else None
    due = 0
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, Mapping) or entry.get("awaiting_action_from") != "user":
            continue
        deadline = entry.get("minimum_deadline") or {}
        if deadline.get("status") in ("currently_due", "past_due"):
            due += 1
    return {
        "charges_enabled": card.get("status") == "active",
        "details_submitted": isinstance(reqs, Mapping) and due == 0,
        "requirements_due_count": due,
    }


def _label(account: Mapping[str, Any]) -> str:
    """A v2 account's name for the connections list."""
    business = (account.get("identity") or {}).get("business_details") or {}
    profile = (account.get("defaults") or {}).get("profile") or {}
    for value in (
        account.get("display_name"),
        profile.get("doing_business_as"),
        business.get("registered_name"),
        account.get("contact_email"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return str(account.get("id") or "Stripe account")


# ------------------------------------------------------------------ checkout


def platform_fee_minor(total_minor: int, currency: str) -> int:  # noqa: ARG001
    """The platform's cut of one checkout, in minor units. 0 while the fee is off."""
    if PLATFORM_FEE_BPS <= 0:
        return 0
    return min(total_minor, total_minor * PLATFORM_FEE_BPS // 10_000)


async def _create_checkout(ctx: ActionContext, args: CheckoutArgs) -> dict[str, Any]:
    if ctx.get_token is None:
        raise ProviderError("stripe checkout without a connection")
    account_id = await ctx.get_token()
    if not account_id.startswith("acct_"):
        raise ProviderError("stripe connection has no account")

    product = await catalog_product(ctx.workspace_id, ctx.pocket_id, args.product_ref)
    if product is None or product.in_stock is False or product.price_cents <= 0:
        raise ActionRefused("invalid_args", "That product isn't available.")
    try:
        currency = normalize_currency(product.currency)
    except ValueError:
        raise ActionRefused("invalid_args", "That product has no valid currency.") from None
    wanted = str(ctx.settings.get("currency") or "").upper()
    if wanted and wanted != currency:
        raise ActionRefused("invalid_args", "That product is priced in another currency.")
    total = product.price_cents * args.qty
    if total > MAX_AMOUNT_MINOR:
        raise ActionRefused("invalid_args", "That order is too large to check out.")

    origin = await site_origin(ctx.site_id)
    if not origin:
        raise ProviderError("site has no public address yet")
    metadata = {
        "site_id": ctx.site_id,
        "pocket_id": ctx.pocket_id,
        "product_ref": product.id,
        "product_name": product.name[:200],
        "qty": args.qty,
        "unit_amount": product.price_cents,
    }
    intent: dict[str, Any] = {"metadata": metadata}
    fee = platform_fee_minor(total, currency)
    if fee > 0:
        intent["application_fee_amount"] = fee
    session = await _stripe(
        "POST",
        "/v1/checkout/sessions",
        {
            "mode": "payment",
            "line_items": [
                {
                    "quantity": args.qty,
                    "price_data": {
                        "currency": currency.lower(),
                        "unit_amount": product.price_cents,
                        "product_data": {"name": product.name[:250] or product.id},
                    },
                }
            ],
            "success_url": origin + _safe_path(ctx.settings.get("success_path")),
            "cancel_url": origin + _safe_path(ctx.settings.get("cancel_path")),
            "metadata": metadata,
            "payment_intent_data": intent,
        },
        account=account_id,
    )
    url = str((session or {}).get("url") or "")
    if not url.startswith("https://"):
        raise ProviderError("stripe returned no checkout url")
    return {"url": url}


# ------------------------------------------------------------------ webhooks


def verify_signature(body: bytes, header: str, secret: str, *, now: float | None = None) -> bool:
    """Stripe's scheme: ``t=<ts>,v1=<hex>``; HMAC-SHA256 over ``"<ts>.<body>"``."""
    if not secret or not header:
        return False
    stamp = ""
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            stamp = value
        elif key == "v1" and value:
            signatures.append(value)
    if not stamp.isdigit() or not signatures:
        return False
    current = time.time() if now is None else now
    if abs(current - int(stamp)) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), stamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """A ``Stripe-Signature`` header value (tests and local tooling)."""
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    mac = hmac.new(secret.encode("utf-8"), stamp.encode() + b"." + body, hashlib.sha256)
    return f"t={stamp},v1={mac.hexdigest()}"


def _session_order(obj: Mapping[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata") or {}
    customer = obj.get("customer_details") or {}
    email = customer.get("email") or obj.get("customer_email")
    try:
        qty = max(1, int(meta.get("qty") or 1))
    except (TypeError, ValueError):
        qty = 1
    return {
        "order_id": str(obj.get("payment_intent") or obj.get("id") or ""),
        "amount_minor": obj.get("amount_total") or 0,
        "currency": str(obj.get("currency") or "").upper(),
        "customer_email": email if isinstance(email, str) else None,
        "items": [
            {
                "ref": str(meta.get("product_ref") or ""),
                "name": str(meta.get("product_name") or ""),
                "qty": qty,
                "amount_minor": obj.get("amount_total") or 0,
            }
        ],
    }


def _fully_refunded(charge: Mapping[str, Any]) -> bool:
    """Only a full refund settles an order as refunded; a partial one leaves it paid."""
    amount, refunded = charge.get("amount"), charge.get("amount_refunded")
    if not isinstance(amount, int) or not isinstance(refunded, int):
        return False
    return amount > 0 and refunded >= amount


def normalize_event(event: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
    """(our type, data, site_id) for a verified Stripe event."""
    kind = str(event.get("type") or "")
    if event.get("object") == "v2.core.event":
        # A thin event names the account, not its state: ``expand_event`` fetches it.
        related = event.get("related_object") or {}
        if kind in ACCOUNT_THIN_EVENTS and related.get("type") == "v2.core.account":
            return ACCOUNT_REFRESH, {}, ""
        return "ignored", {}, ""
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, Mapping):
        return "ignored", {}, ""
    site_id = str((obj.get("metadata") or {}).get("site_id") or "")
    if kind == "account.updated":
        created = event.get("created")
        as_of = created if isinstance(created, int) and not isinstance(created, bool) else 0
        return "connection.updated", {"meta": _meta(obj), "as_of": as_of}, ""
    if kind == "checkout.session.completed":
        paid = obj.get("payment_status") in ("paid", "no_payment_required")
        return ("order.paid" if paid else "order.pending"), _session_order(obj), site_id
    if kind == "checkout.session.async_payment_succeeded":
        return "order.paid", _session_order(obj), site_id
    if kind == "checkout.session.async_payment_failed":
        return "order.failed", {**_session_order(obj), "only_if_exists": True}, site_id
    if kind == "checkout.session.expired":
        order_id = str(obj.get("payment_intent") or obj.get("id") or "")
        return "order.failed", {"order_id": order_id, "only_if_exists": True}, site_id
    if kind == "payment_intent.payment_failed":
        return (
            "order.failed",
            {"order_id": str(obj.get("id") or ""), "only_if_exists": True},
            (site_id),
        )
    if kind == "charge.refunded" and obj.get("payment_intent") and _fully_refunded(obj):
        # No site_id on a charge is fine: the ingest finds the order by its id.
        data = {"order_id": str(obj["payment_intent"]), "only_if_exists": True}
        return "order.refunded", data, site_id
    return "ignored", {}, ""


# ------------------------------------------------------------------ provider


class StripeProvider(BaseProvider):
    id = "stripe"
    name = "Stripe"
    kind = "connect"
    category = "payments"
    plan = "site"
    supports_pkce = True
    state_ttl_seconds = FLOW_TTL_SECONDS
    actions = {
        "create_checkout": ActionSpec(
            args=CheckoutArgs,
            handler=_create_checkout,
            description="Start a Stripe Checkout for one catalog product; returns its url.",
            public=True,
            write=True,
        ),
    }
    settings_fields = (
        SettingsField(
            key="currency",
            label="Currency",
            type="select",
            help="Checkout only sells products priced in this currency. Empty: each product's own.",
            options=tuple(SettingOption(c, c) for c in CURRENCIES),
        ),
        SettingsField(
            key="success_path",
            label="After payment, send buyers to",
            help="A path on your site, like /thanks. Defaults to your home page.",
            max_length=MAX_PATH,
        ),
        SettingsField(
            key="cancel_path",
            label="If they cancel, send them to",
            help="A path on your site, like /shop. Defaults to your home page.",
            max_length=MAX_PATH,
        ),
    )

    def availability(self) -> Availability:
        return "available" if secret_key() else "unconfigured"

    async def authorize_url(  # type: ignore[override]
        self, state: str, redirect_uri: str, code_challenge: str | None = None
    ) -> str:
        flow = await _flow(state)
        workspace_id, user_id = str(flow["workspace_id"]), str(flow["user_id"])
        account_id = await _pending_account(workspace_id, user_id)
        if not account_id:
            account_id = await _create_account(workspace_id, user_id)
            await _redis().setex(
                _pending_key(workspace_id, user_id), PENDING_TTL_SECONDS, account_id
            )
        await _bind(flow, account_id)
        return await _account_link(account_id, state, redirect_uri)

    async def complete(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> TokenGrant:
        if not code_verifier:
            raise ProviderError("stripe flow has no verifier")
        binding = await _take_bound(code_verifier)
        bound = binding.get("account", "")
        if not bound or not hmac.compare_digest(bound.encode(), (code or "").encode()):
            raise ProviderError("stripe account does not match this flow")
        account = await _get_account(bound) or {}
        if account.get("id") != bound:
            raise ProviderError("stripe returned another account")
        if not _owned_by(account, binding.get("workspace_id", ""), binding.get("user_id", "")):
            raise ProviderError("stripe account belongs to another workspace")
        return TokenGrant(
            access_token=bound,
            account_ref=bound,
            account_label=_label(account),
            token_type="stripe_account",
            meta=_v2_meta(account),
        )

    async def resume_url(self, connection: Any, state: str, redirect_uri: str) -> str | None:
        meta = connection.meta or {}
        account_id = str(connection.account_ref or "")
        if not account_id.startswith("acct_"):
            return None
        if meta.get("charges_enabled") and not meta.get("requirements_due_count"):
            return None
        flow = await _flow(state)
        if str(flow.get("workspace_id") or "") != connection.workspace:
            raise ProviderError("stripe resume for another workspace")
        await _bind(flow, account_id, owner=str(connection.created_by or ""))
        return await _account_link(account_id, state, redirect_uri)

    def connection_attention(self, connection: Any) -> str | None:
        meta = connection.meta or {}
        if not meta.get("charges_enabled"):
            return "Finish setting up your Stripe account"
        if meta.get("requirements_due_count"):
            return "Stripe needs more details about your account"
        return None

    def site_status(self, binding: Any, connection: Any | None) -> str | None:  # noqa: ARG002
        if connection is not None and not (connection.meta or {}).get("charges_enabled"):
            return "setup_required"
        return None

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> NormalizedEvent | None:
        header = headers.get("stripe-signature", "")
        connect = verify_signature(body, header, webhook_secret())
        if not connect and not verify_signature(body, header, accounts_webhook_secret()):
            return None
        try:
            event = json.loads(body)
        except ValueError:
            return None
        if not isinstance(event, dict) or not event.get("id"):
            return None
        kind, data, site_id = normalize_event(event)
        account_ref = str(event.get("account") or "")
        if kind == ACCOUNT_REFRESH:
            account_ref = str((event.get("related_object") or {}).get("id") or "")
        return NormalizedEvent(
            id=str(event["id"]),
            type=kind,
            provider=self.id,
            data=data,
            site_id=site_id,
            account_ref=account_ref,
        )

    async def expand_event(self, event: NormalizedEvent) -> NormalizedEvent:
        """A thin account event becomes ``connection.updated`` with the account's
        current state, stamped with the fetch time so an older snapshot event that
        arrives later can't undo it. A ProviderError here is retried by Stripe."""
        if event.type != ACCOUNT_REFRESH:
            return event
        account_id = event.account_ref
        account = await _get_account(account_id, missing_ok=True) if account_id else None
        if not account or account.get("id") != account_id:
            return NormalizedEvent(id=event.id, type="ignored", provider=self.id)
        return NormalizedEvent(
            id=event.id,
            type="connection.updated",
            provider=self.id,
            data={"meta": _v2_meta(account), "as_of": int(time.time())},
            account_ref=account_id,
        )
