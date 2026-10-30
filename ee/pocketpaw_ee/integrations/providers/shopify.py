# ee/pocketpaw_ee/integrations/providers/shopify.py: the ``shopify`` commerce provider.
#
# Kind ``connect`` (Shopify public app, authorization code grant), category
# ``commerce``, plan ``site``. Configured by ``POCKETPAW_INTEGRATIONS_SHOPIFY_
# CLIENT_ID`` / ``_SECRET``; without both it is ``unconfigured``. The owner names the
# shop before Connect (``connect_params`` ``shop``: lowercase ``<name>.myshopify.com``,
# ``SHOP_RE.fullmatch``). It rides the server-side OAuth state and the connection's
# ``meta["connect_params"]``, never the callback query. ``verify_callback`` checks
# the callback ``hmac`` (hex HMAC-SHA256, client secret, sorted query with Shopify's
# escaping) and the exact ``shop`` before the code is spent. Tokens are expiring
# offline tokens (``expiring=1``): access ~1h, refresh ~90d, rotated on every
# refresh (the vault keeps the newest; ``refresh_with_meta`` gives it the shop); a
# refused refresh (401) is ``InvalidGrant``.
# ``account_ref`` = shop domain, ``account_label`` = shop name.
#
# ``site_setup`` (binding lease, idempotent) mints one Storefront token per shop with
# ``storefrontAccessTokenCreate``, Fernet-encrypted in ``secret_ciphertext`` (never
# in settings or a view); hidden settings hold its id, the shop and the checkout
# hosts; ``shop_domain`` is the readonly link. ACCESS_DENIED -> setup_required
# (``storefront_access_denied``). A shop change clears the old token locally first,
# then deletes it on Shopify (best effort); unlinking deletes it too.
#
# Public actions on the Storefront API; no price comes from the caller.
# ``list_products {limit 1..50, cursor?}`` (minor units, cached 60s per site, shop
# and page); ``create_cart_checkout {variant_id, qty 1..20}`` -> ``{url}``, only https
# on the shop, its primary domain (re-read on a miss) or checkout.shopify.com.
#
# Webhooks (``X-Shopify-Hmac-Sha256`` over the raw body, deduped by webhook id) go
# to ``handle_event``: app/uninstalled drops the grant (connection and sites read
# needs_reconnect) unless the connection changed after ``X-Shopify-Triggered-At``;
# shop/redact purges the shop's needs_reconnect / revoked connections, never an
# active one; customers/* are acked (we store no Shopify customer data).

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import Field, field_validator

from pocketpaw.money import normalize_currency, to_minor
from pocketpaw_ee.integrations.providers import oauth_http
from pocketpaw_ee.integrations.providers.base import (
    ActionArgs,
    ActionContext,
    ActionRefused,
    ActionSpec,
    Availability,
    BaseProvider,
    ConnectParam,
    InvalidGrant,
    NormalizedEvent,
    ProviderError,
    SettingsField,
    SetupContext,
    TokenGrant,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "shopify"
ENV_PREFIX = "POCKETPAW_INTEGRATIONS_SHOPIFY"
API_VERSION = "2026-10"
SCOPES = (
    "unauthenticated_read_product_listings",
    "unauthenticated_write_checkouts",
    "read_products",
)
# Without these the storefront token can't list or sell.
REQUIRED_SCOPES = ("unauthenticated_read_product_listings", "unauthenticated_write_checkouts")
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.myshopify\.com$")
CHECKOUT_HOST = "checkout.shopify.com"
SHOP_DOMAIN = "shop_domain"
STOREFRONT_SHOP = "storefront_shop"
CHECKOUT_HOSTS = "checkout_hosts"
STOREFRONT_TOKEN_ID = "storefront_token_id"
CACHE_SECONDS = 60.0
CACHE_MAX = 512
MAX_VARIANTS = 20
_VARIANT_GID = re.compile(r"^gid://shopify/ProductVariant/(\d{1,20})$")

_CacheKey = tuple[str, str, str, int, str]
# (workspace, pocket, shop, limit, cursor) -> (expires at, result)
_cache: dict[_CacheKey, tuple[float, dict[str, Any]]] = {}


def _credentials() -> tuple[str, str]:
    client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
    if not client_id or not secret:
        raise ProviderError("shopify: not configured")
    return client_id, secret


def valid_shop(value: Any) -> str:
    """``value`` as a lowercase ``<name>.myshopify.com``; ValueError otherwise."""
    shop = str(value or "").strip().lower()
    if not SHOP_RE.fullmatch(shop):
        raise ValueError("Enter your store's address, like your-store.myshopify.com.")
    return shop


def _shop_of(params: Mapping[str, Any] | None) -> str:
    try:
        return valid_shop((params or {}).get("shop"))
    except ValueError:
        raise ProviderError("shopify: no valid shop for this flow") from None


def _host(value: Any) -> str:
    return str(value or "").strip().lower().rstrip(".")


def _escape(value: str, *, key: bool = False) -> str:
    """Shopify's OAuth hmac escaping: ``%`` and ``&`` always, ``=`` in keys."""
    value = value.replace("%", "%25").replace("&", "%26")
    return value.replace("=", "%3D") if key else value


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- OAuth


async def _token_post(shop: str, data: dict[str, str], *, refreshing: bool) -> dict[str, Any]:
    url = f"https://{shop}/admin/oauth/access_token"
    try:
        async with oauth_http.client() as http:
            resp = await http.post(url, data=data, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ProviderError(f"shopify: token endpoint unreachable ({type(exc).__name__})") from None
    body = oauth_http.body_of(resp)
    if resp.status_code >= 400:
        reason = str(body.get("error") or "").lower()
        # Shopify answers every dead refresh token (spent, expired, app uninstalled)
        # with 401 ``invalid_request``.
        if refreshing and (
            resp.status_code == 401 or reason in {"invalid_request", "invalid_grant"}
        ):
            raise InvalidGrant(f"shopify: {reason or resp.status_code}")
        raise ProviderError(f"shopify: token endpoint answered {resp.status_code}")
    if not body.get("access_token"):
        raise ProviderError("shopify: token response had no access token")
    return body


async def _admin(
    get_token: Any, shop: str, query: str, variables: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    """One Admin GraphQL call with ``X-Shopify-Access-Token``; a 401 refreshes once."""
    if get_token is None:
        raise ProviderError("shopify: no connection")
    url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    token = await get_token()
    async with oauth_http.client() as http:
        for attempt in (0, 1):
            try:
                resp = await http.post(
                    url,
                    json={"query": query, "variables": variables or {}},
                    headers={"x-shopify-access-token": token, "accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"shopify: Admin API unreachable ({type(exc).__name__})"
                ) from None
            if resp.status_code != 401 or attempt:
                return resp.status_code, oauth_http.body_of(resp)
            token = await get_token(rejected=token)
    raise ProviderError("shopify: unreachable")  # pragma: no cover - loop returns


def _error_codes(body: Mapping[str, Any]) -> set[str]:
    errors = body.get("errors")
    if not isinstance(errors, list):
        return set()
    return {
        str((e.get("extensions") or {}).get("code") or "").upper()
        for e in errors
        if isinstance(e, dict)
    }


_SHOP_QUERY = "query { shop { name myshopifyDomain primaryDomain { host } } }"
_MINT = """mutation Mint($input: StorefrontAccessTokenInput!) {
  storefrontAccessTokenCreate(input: $input) {
    storefrontAccessToken { id accessToken }
    userErrors { field message }
  }
}"""
_DELETE = """mutation Drop($input: StorefrontAccessTokenDeleteInput!) {
  storefrontAccessTokenDelete(input: $input) {
    deletedStorefrontAccessTokenId
    userErrors { field message }
  }
}"""


async def _delete_storefront_token(conn: Any, shop: str, token_id: str) -> None:
    """Delete a storefront token on Shopify with ``conn``'s grant. Never raises:
    the local copy is already gone, a token left behind only counts toward the
    shop's 100-token limit."""
    if not token_id or conn is None or conn.status != "active" or conn.account_ref != shop:
        return
    from pocketpaw_ee.integrations import vault

    async def _token(rejected: str = "") -> str:
        return await vault.get_valid_token(conn, rejected=rejected)

    try:
        status, body = await _admin(_token, shop, _DELETE, {"input": {"id": token_id}})
        payload = ((body.get("data") or {}).get("storefrontAccessTokenDelete")) or {}
        if status >= 400 or _error_codes(body) or payload.get("userErrors"):
            logger.info("shopify: storefront token delete answered %s", status)
    except Exception as exc:  # noqa: BLE001 - best effort
        logger.info("shopify: storefront token delete failed: %s", type(exc).__name__)


async def _active_connection(workspace_id: str, shop: str):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection as Conn

    return await Conn.find_one(
        Conn.workspace == workspace_id,
        Conn.provider == PROVIDER_ID,
        Conn.account_ref == shop,
        Conn.status == "active",
    )


def _shop_info(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    if status >= 400 or "ACCESS_DENIED" in _error_codes(body):
        raise ProviderError(f"shopify: shop query answered {status}")
    shop = ((body.get("data") or {}).get("shop")) or {}
    if not isinstance(shop, dict):
        return {}
    return shop


# ---------------------------------------------------------------- actions


class ListProductsArgs(ActionArgs):
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=512)


class CartCheckoutArgs(ActionArgs):
    variant_id: str = Field(max_length=80)
    qty: int = Field(default=1, ge=1, le=20)

    @field_validator("variant_id")
    @classmethod
    def _gid(cls, value: str) -> str:
        value = value.strip()
        if value.isdigit() and len(value) <= 20:
            return f"gid://shopify/ProductVariant/{value}"
        if not _VARIANT_GID.match(value):
            raise ValueError("variant_id must be a Shopify product variant id.")
        return value


async def _storefront_creds(ctx: ActionContext) -> tuple[str, str]:
    """(shop, storefront token) from the site's binding; setup_required when absent."""
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.cloud.models.integration import SiteIntegration as Doc

    row = await Doc.find_one(
        Doc.workspace == ctx.workspace_id,
        Doc.pocket_id == ctx.pocket_id,
        Doc.provider == PROVIDER_ID,
    )
    shop = str(((row.settings or {}) if row is not None else {}).get(STOREFRONT_SHOP) or "")
    if row is None or not row.secret_ciphertext or not SHOP_RE.fullmatch(shop):
        # Keep the reason setup stored (e.g. storefront_access_denied) for the owner.
        reason = getattr(row, "setup_error", "") or "storefront_token_missing"
        raise ActionRefused("setup_required", setup_error=reason)
    try:
        token = crypto.decrypt(row.secret_ciphertext)
    except Exception:  # noqa: BLE001 - an unreadable secret is a missing one
        token = ""
    if not token:
        raise ActionRefused("setup_required", setup_error="storefront_token_missing")
    return shop, token


async def _storefront(
    shop: str, token: str, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One Storefront GraphQL call; the ``data`` object, or a refusal / ProviderError."""
    url = f"https://{shop}/api/{API_VERSION}/graphql.json"
    try:
        async with oauth_http.client() as http:
            resp = await http.post(
                url,
                json={"query": query, "variables": variables or {}},
                headers={"x-shopify-storefront-access-token": token, "accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise ProviderError(f"shopify: Storefront API unreachable ({type(exc).__name__})") from None
    if resp.status_code in (401, 403):
        raise ActionRefused("setup_required", setup_error="storefront_token_rejected")
    if resp.status_code in (429, 430):
        raise ActionRefused("rate_limited")
    if resp.status_code >= 400:
        raise ProviderError(f"shopify: Storefront API answered {resp.status_code}")
    body = oauth_http.body_of(resp)
    codes = _error_codes(body)
    if "THROTTLED" in codes:
        raise ActionRefused("rate_limited")
    if "ACCESS_DENIED" in codes:
        raise ActionRefused("setup_required", setup_error="storefront_access_denied")
    data = body.get("data")
    if codes or not isinstance(data, dict):
        raise ProviderError("shopify: Storefront API returned errors")
    return data


def _money(price: Any) -> tuple[int | None, str]:
    if not isinstance(price, dict):
        return None, ""
    currency = normalize_currency(price.get("currencyCode")) if price.get("currencyCode") else ""
    try:
        return to_minor(str(price.get("amount")), currency), currency
    except ValueError:
        return None, currency


def _nodes(conn: Any) -> list[dict[str, Any]]:
    nodes = (conn or {}).get("nodes") if isinstance(conn, dict) else None
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def _product(node: dict[str, Any]) -> dict[str, Any]:
    price_minor, currency = _money((node.get("priceRange") or {}).get("minVariantPrice"))
    variants = []
    for v in _nodes(node.get("variants")):
        v_minor, v_currency = _money(v.get("price"))
        currency = currency or v_currency
        variants.append(
            {
                "id": str(v.get("id") or ""),
                "title": str(v.get("title") or ""),
                "price_minor": v_minor,
                "available": bool(v.get("availableForSale")),
            }
        )
    image = node.get("featuredImage") or {}
    return {
        "id": str(node.get("id") or ""),
        "title": str(node.get("title") or ""),
        "handle": str(node.get("handle") or ""),
        "image_url": str(image.get("url"))
        if isinstance(image, dict) and image.get("url")
        else None,
        "price_minor": price_minor,
        "currency": currency,
        "variants": variants,
    }


_PRODUCTS = """query Products($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title handle
      featuredImage { url }
      priceRange { minVariantPrice { amount currencyCode } }
      variants(first: VARIANTS) {
        nodes { id title availableForSale price { amount currencyCode } }
      }
    }
  }
}""".replace("VARIANTS", str(MAX_VARIANTS))


def _cache_get(key: _CacheKey) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    if hit[0] <= time.monotonic():
        _cache.pop(key, None)
        return None
    return hit[1]


def _cache_put(key: _CacheKey, value: dict[str, Any]) -> None:
    now = time.monotonic()
    if len(_cache) >= CACHE_MAX:
        for k in [k for k, (exp, _) in _cache.items() if exp <= now] or list(_cache)[:64]:
            _cache.pop(k, None)
    _cache[key] = (now + CACHE_SECONDS, value)


def clear_cache() -> None:
    _cache.clear()


async def _list_products(ctx: ActionContext, args: ListProductsArgs) -> dict[str, Any]:
    settings_shop = str(ctx.settings.get(STOREFRONT_SHOP) or "")
    key = (ctx.workspace_id, ctx.pocket_id, settings_shop, args.limit, args.cursor or "")
    cached = _cache_get(key) if settings_shop else None
    if cached is not None:
        return cached
    shop, token = await _storefront_creds(ctx)
    key = (ctx.workspace_id, ctx.pocket_id, shop, args.limit, args.cursor or "")
    data = await _storefront(shop, token, _PRODUCTS, {"first": args.limit, "after": args.cursor})
    products = data.get("products") or {}
    page = products.get("pageInfo") or {} if isinstance(products, dict) else {}
    result = {
        "products": [_product(n) for n in _nodes(products)][: args.limit],
        "next_cursor": (
            str(page.get("endCursor"))
            if page.get("hasNextPage") and page.get("endCursor")
            else None
        ),
    }
    _cache_put(key, result)
    return result


_CART = """mutation Cart($lines: [CartLineInput!]!) {
  cartCreate(input: { lines: $lines }) {
    cart { checkoutUrl }
    userErrors { code field message }
  }
}"""
_PRIMARY = "query { shop { primaryDomain { host } } }"


def _checkout_host(url: str) -> str | None:
    """The host of an https checkout URL with no credentials or port, else None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port is not None:
        return None
    return _host(parts.hostname) or None


async def _create_cart_checkout(ctx: ActionContext, args: CartCheckoutArgs) -> dict[str, Any]:
    shop, token = await _storefront_creds(ctx)
    data = await _storefront(
        shop, token, _CART, {"lines": [{"merchandiseId": args.variant_id, "quantity": args.qty}]}
    )
    payload = data.get("cartCreate") or {}
    if payload.get("userErrors"):
        raise ActionRefused("invalid_args", "That product can't be bought right now.")
    url = str(((payload.get("cart") or {}).get("checkoutUrl")) or "")
    host = _checkout_host(url)
    if host is None:
        raise ProviderError("shopify: checkout URL is not https")
    allowed = {shop, CHECKOUT_HOST}
    allowed.update(h for h in str(ctx.settings.get(CHECKOUT_HOSTS) or "").split(",") if h)
    if host not in allowed:
        # The owner may have changed the shop's primary domain since setup.
        live = await _storefront(shop, token, _PRIMARY)
        primary = _host(((live.get("shop") or {}).get("primaryDomain") or {}).get("host"))
        if host != primary:
            raise ProviderError("shopify: checkout URL is on an unexpected host")
    return {"url": url}


# ---------------------------------------------------------------- provider


class ShopifyProvider(BaseProvider):
    id = PROVIDER_ID
    name = "Shopify"
    kind = "connect"
    category = "commerce"
    plan = "site"
    supports_pkce = False
    # The token endpoint is the shop's own host: refresh reads it from the grant's meta.
    refresh_with_meta = True
    connect_params = (
        ConnectParam(
            key="shop",
            label="Shopify store address",
            placeholder="your-store.myshopify.com",
            max_length=100,
        ),
    )
    settings_fields = (
        SettingsField(
            key=SHOP_DOMAIN,
            label="Shopify store",
            type="link",
            readonly=True,
            help="The store this site sells from.",
        ),
        SettingsField(key=STOREFRONT_SHOP, label="Storefront shop", hidden=True, max_length=100),
        SettingsField(key=CHECKOUT_HOSTS, label="Checkout hosts", hidden=True, max_length=600),
        SettingsField(
            key=STOREFRONT_TOKEN_ID, label="Storefront token id", hidden=True, max_length=200
        ),
    )
    actions = {
        "list_products": ActionSpec(
            args=ListProductsArgs,
            handler=_list_products,
            description="List the store's products with prices and variants (a product grid).",
            public=True,
        ),
        "create_cart_checkout": ActionSpec(
            args=CartCheckoutArgs,
            handler=_create_cart_checkout,
            description="Start a Shopify checkout for one variant (a buy button); returns its url.",
            public=True,
            ip_per_min=20,
            site_per_min=300,
        ),
    }

    def availability(self) -> Availability:
        client_id, secret = oauth_http.client_credentials(ENV_PREFIX)
        return "available" if client_id and secret else "unconfigured"

    def check_connect_params(self, params: Mapping[str, Any]) -> dict[str, str]:
        checked = super().check_connect_params(params)
        return {"shop": valid_shop(checked["shop"])}

    def authorize_url(
        self,
        state: str,
        redirect_uri: str,
        code_challenge: str | None = None,  # noqa: ARG002 - no PKCE
        *,
        params: Mapping[str, Any] | None = None,
    ) -> str:
        client_id, _ = _credentials()
        shop = _shop_of(params)
        query = {
            "client_id": client_id,
            "scope": ",".join(SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"https://{shop}/admin/oauth/authorize?{urlencode(query)}"

    def verify_callback(self, query: list[tuple[str, str]], params: Mapping[str, Any]) -> bool:
        """Shopify's signed redirect: hex HMAC-SHA256 (client secret) over the query
        minus ``hmac``, sorted, ``k=v`` joined by ``&`` with Shopify's escaping (``%``
        and ``&`` in values; ``%``, ``&`` and ``=`` in keys); and ``shop`` is exactly
        the state's."""
        try:
            _, secret = _credentials()
            shop = _shop_of(params)
        except ProviderError:
            return False
        keys = [k for k, _ in query]
        if len(keys) != len(set(keys)):
            return False  # a repeated key makes the signed message ambiguous
        values = dict(query)
        given = values.pop("hmac", "")
        values.pop("signature", None)
        if not given or values.get("shop") != shop:
            return False
        message = "&".join(
            f"{_escape(k, key=True)}={_escape(v)}" for k, v in sorted(values.items())
        )
        expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, given.lower())

    async def complete(
        self,
        code: str,
        redirect_uri: str,  # noqa: ARG002 - Shopify's token call doesn't take it
        code_verifier: str | None = None,  # noqa: ARG002 - no PKCE
        *,
        params: Mapping[str, Any] | None = None,
    ) -> TokenGrant:
        client_id, secret = _credentials()
        shop = _shop_of(params)
        body = await _token_post(
            shop,
            {"client_id": client_id, "client_secret": secret, "code": code, "expiring": "1"},
            refreshing=False,
        )
        scopes = [s.strip() for s in str(body.get("scope") or "").split(",") if s.strip()]
        missing = [s for s in REQUIRED_SCOPES if s not in scopes]
        if missing:
            raise ProviderError(f"shopify: the grant is missing {missing[0]}")
        access = str(body["access_token"])

        async def _token(rejected: str = "") -> str:  # noqa: ARG001 - fresh token
            return access

        info = _shop_info(*await _admin(_token, shop, _SHOP_QUERY))
        return TokenGrant(
            access_token=access,
            refresh_token=str(body.get("refresh_token") or ""),
            expires_in=_int(body.get("expires_in")),
            scopes=scopes,
            account_label=str(info.get("name") or shop)[:200],
            account_ref=shop,
            meta={"connect_params": {"shop": shop}},
        )

    async def refresh(
        self, refresh_token: str, *, meta: Mapping[str, Any] | None = None
    ) -> TokenGrant:
        client_id, secret = _credentials()
        shop = _shop_of((meta or {}).get("connect_params"))
        body = await _token_post(
            shop,
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

    # ------------------------------------------------------------ site setup

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:
        """Mint (once per shop) the site's Storefront token; set the shop link."""
        from pocketpaw_ee.cloud._core import crypto
        from pocketpaw_ee.integrations import bindings

        conn = ctx.connection
        try:
            shop = valid_shop(getattr(conn, "account_ref", ""))
        except ValueError:
            raise ProviderError("shopify: connection has no shop") from None
        info = _shop_info(*await _admin(ctx.get_token, shop, _SHOP_QUERY))
        hosts = [
            h for h in dict.fromkeys([_host((info.get("primaryDomain") or {}).get("host"))]) if h
        ]
        written = {
            SHOP_DOMAIN: f"https://{shop}",
            STOREFRONT_SHOP: shop,
            CHECKOUT_HOSTS: ",".join(h for h in hosts if h != shop),
        }
        lease = await bindings.claim_binding_lease(binding.workspace, binding.pocket_id, self.id)
        if lease is None:
            raise ProviderError("shopify: setup is busy")
        owner, row = lease
        update: dict[str, Any] = {}
        try:
            stored_shop = str((row.get("settings") or {}).get(STOREFRONT_SHOP) or "")
            if row.get("secret_ciphertext") and stored_shop == shop:
                binding.secret_ciphertext = row["secret_ciphertext"]
                stored_id = (row.get("settings") or {}).get(STOREFRONT_TOKEN_ID)
                return {**written, STOREFRONT_TOKEN_ID: str(stored_id or "")}
            if row.get("secret_ciphertext") or stored_shop:
                # Another shop's token: clear it here first (it stays cleared if the
                # new mint fails), then delete it on that shop, best effort.
                update = {
                    "secret_ciphertext": "",
                    f"settings.{STOREFRONT_SHOP}": "",
                    f"settings.{STOREFRONT_TOKEN_ID}": "",
                }
                binding.secret_ciphertext = ""
                old_id = str((row.get("settings") or {}).get(STOREFRONT_TOKEN_ID) or "")
                if stored_shop and old_id:
                    old_conn = await _active_connection(binding.workspace, stored_shop)
                    await _delete_storefront_token(old_conn, stored_shop, old_id)
            status, body = await _admin(
                ctx.get_token, shop, _MINT, {"input": {"title": "Paw Sites"}}
            )
            if status == 403 or "ACCESS_DENIED" in _error_codes(body):
                raise ActionRefused(
                    "setup_required",
                    "Shopify refused to create a storefront token.",
                    setup_error="storefront_access_denied",
                )
            payload = ((body.get("data") or {}).get("storefrontAccessTokenCreate")) or {}
            minted = payload.get("storefrontAccessToken") or {}
            token = str(minted.get("accessToken") or "")
            if status >= 400 or payload.get("userErrors") or not token:
                raise ProviderError(f"shopify: storefront token mint answered {status}")
            update = {
                "secret_ciphertext": crypto.encrypt(token),
                f"settings.{STOREFRONT_SHOP}": shop,
                f"settings.{STOREFRONT_TOKEN_ID}": str(minted.get("id") or ""),
            }
            binding.secret_ciphertext = update["secret_ciphertext"]
        finally:
            await bindings.release_binding_lease(
                binding.workspace, binding.pocket_id, self.id, owner, update
            )
        clear_cache()
        return {**written, STOREFRONT_TOKEN_ID: update.get(f"settings.{STOREFRONT_TOKEN_ID}", "")}

    async def site_teardown(self, binding: Any) -> None:
        """The site was unlinked or deleted: delete its storefront token on Shopify."""
        from pocketpaw_ee.integrations import bindings

        settings = dict(binding.settings or {})
        token_id = str(settings.get(STOREFRONT_TOKEN_ID) or "")
        shop = str(settings.get(STOREFRONT_SHOP) or "")
        if token_id and SHOP_RE.fullmatch(shop):
            conn = await bindings.connection_for(binding)
            await _delete_storefront_token(conn, shop, token_id)
        clear_cache()

    def site_status(self, binding: Any, connection: Any | None) -> str | None:
        if connection is not None and connection.status == "active":
            if not binding.secret_ciphertext:
                return "setup_required"
        return None

    def connection_attention(self, connection: Any) -> str | None:
        if connection.status == "needs_reconnect" and connection.last_error == "app_uninstalled":
            return "The app was removed from this Shopify store. Reconnect to sell again."
        return None

    # -------------------------------------------------------------- webhooks

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> NormalizedEvent | None:
        try:
            _, secret = _credentials()
            given = base64.b64decode(headers.get("x-shopify-hmac-sha256", ""), validate=True)
        except (ProviderError, ValueError):
            return None
        expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
        if not given or not hmac.compare_digest(expected, given):
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        topic = str(headers.get("x-shopify-topic") or "").strip().lower()
        # The topic header is not signed: each topic must carry its own body shape,
        # so a signed body of one kind can't be replayed as another.
        if topic == "app/uninstalled":
            raw_shop, ok = payload.get("myshopify_domain"), "myshopify_domain" in payload
        elif topic == "shop/redact":
            raw_shop = payload.get("shop_domain")
            ok = "customer" not in payload and "shop_id" in payload
        elif topic in ("customers/redact", "customers/data_request"):
            raw_shop, ok = payload.get("shop_domain"), isinstance(payload.get("customer"), dict)
        else:
            raw_shop, ok = payload.get("shop_domain") or payload.get("myshopify_domain"), True
        if not ok:
            return None
        try:
            shop = valid_shop(raw_shop)
        except ValueError:
            return None
        event_id = (
            headers.get("x-shopify-webhook-id")
            or headers.get("x-shopify-event-id")
            or hashlib.sha256(body).hexdigest()
        )
        return NormalizedEvent(
            id=str(event_id)[:200],
            type=topic.replace("/", ".") or "unknown",
            provider=self.id,
            account_ref=shop,
            data={"triggered_at": str(headers.get("x-shopify-triggered-at") or "")[:64]},
        )

    async def handle_event(self, event: NormalizedEvent) -> bool:
        if event.type == "app.uninstalled":
            await _uninstalled(event.account_ref, _instant(event.data.get("triggered_at")))
        elif event.type == "shop.redact":
            await _purge_shop(event.account_ref)
        else:
            # customers/data_request, customers/redact: we keep no Shopify customer
            # data (carts and checkouts live on Shopify), so there is nothing to send
            # or erase. Other topics are not subscribed; ack them.
            logger.info("shopify: %s acked for a shop", event.type)
        return True


# --------------------------------------------------------- account events


_INSTANT = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?")


def _instant(value: Any) -> datetime | None:
    """``X-Shopify-Triggered-At`` (ISO 8601, up to nanoseconds) as aware UTC."""
    m = _INSTANT.fullmatch(str(value or "").strip())
    if m is None:
        return None
    zone = "+00:00" if m[3] in (None, "Z") else m[3]
    try:
        return datetime.fromisoformat(m[1] + (m[2] or "")[:7] + zone).astimezone(UTC)
    except ValueError:
        return None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _connections(shop: str, statuses: tuple[str, ...]) -> list[Any]:
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection as Conn

    return await Conn.find(
        Conn.provider == PROVIDER_ID,
        Conn.account_ref == shop,
        {"status": {"$in": list(statuses)}},
    ).to_list()


async def _forget_storefront(connection_id: str, *, unlink: bool) -> list[Any]:
    """Drop the storefront token (and, on ``unlink``, the link) of every bound site."""
    from pocketpaw_ee.cloud.models.integration import SiteIntegration
    from pocketpaw_ee.integrations import bindings

    rows = await bindings.bindings_for_connection(connection_id)
    update: dict[str, Any] = {
        "$set": {"secret_ciphertext": ""},
        "$unset": {f"settings.{STOREFRONT_SHOP}": "", f"settings.{STOREFRONT_TOKEN_ID}": ""},
    }
    if unlink:
        update["$set"]["connection_id"] = None
        update["$unset"].update({f"settings.{SHOP_DOMAIN}": "", f"settings.{CHECKOUT_HOSTS}": ""})
    coll = SiteIntegration.get_pymongo_collection()
    for row in rows:
        await coll.update_one({"_id": row.id}, update)
    return rows


async def _uninstalled(shop: str, triggered_at: datetime | None) -> None:
    """The grant died with the app: keep the row so the owner can reconnect. A
    connection written after the uninstall fired (a reconnect) is left alone."""
    from pocketpaw_ee.integrations import bindings

    for conn in await _connections(shop, ("active", "needs_reconnect")):
        updated = _utc(conn.updatedAt)
        if triggered_at is not None and updated is not None and updated > triggered_at:
            logger.info("shopify: uninstall predates a reconnect; ignored")
            continue
        conn.status = "needs_reconnect"
        conn.token_ciphertext = ""
        conn.expires_at = None
        conn.last_error = "app_uninstalled"
        await conn.save()
        await _forget_storefront(str(conn.id), unlink=False)
        await bindings.notify_connection_status(conn, "needs_reconnect")
    clear_cache()


async def _purge_shop(shop: str) -> None:
    """shop/redact (48h after uninstall): forget the shop entirely."""
    from pocketpaw_ee.integrations import bindings

    # Never an active grant: a shop that reinstalled through Paw keeps it.
    for conn in await _connections(shop, ("needs_reconnect", "revoked")):
        rows = await _forget_storefront(str(conn.id), unlink=True)
        await conn.delete()
        for row in rows:
            try:
                pocket = await bindings.pocket_in_workspace(row.workspace, row.pocket_id)
            except Exception:  # noqa: BLE001 - a gone pocket needs no event
                continue
            await bindings.emit_changed(pocket, row.provider, "needs_connection", "")
    clear_cache()
