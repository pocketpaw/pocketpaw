# tests/ee/sites/test_integrations_shopify.py: the ``shopify`` commerce provider.
#
# Runs end to end through the real routes (connect with a ``shop`` param -> /start
# -> Shopify's authorize page -> signed callback -> site link -> storefront token
# mint -> public gateway -> webhooks) against an in-memory fake of one shop's
# OAuth, Admin GraphQL and Storefront GraphQL endpoints, swapped in as
# ``oauth_http._transport`` (an ``httpx.MockTransport``). No network anywhere.
#
# Covers: shop validation, callback hmac valid / invalid / shop mismatch, expiring
# tokens (expiring=1) with refresh rotation and a dead refresh, storefront token
# mint + idempotence + ACCESS_DENIED, list_products mapping / caps / cache, cart
# checkout URL validation, and the uninstall + GDPR webhooks.
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    _client,
    _code,
    _env,
    _gw,
    _message,
    _pocket,
    _site,
)

CLIENT_ID = "shopify-client-id"
SECRET = "shopify-secret"
SHOP = "acme-store.myshopify.com"
OTHER = "other-store.myshopify.com"
API = "/admin/api/2026-10/graphql.json"
STOREFRONT = "/api/2026-10/graphql.json"
SCOPE = "unauthenticated_read_product_listings,unauthenticated_write_checkouts,read_products"


class FakeShop:
    """One shop's OAuth token endpoint, Admin GraphQL and Storefront GraphQL."""

    def __init__(self, host: str = SHOP) -> None:
        self.host = host
        self.prefix = "" if host == SHOP else "b-"
        self.deleted: list[str] = []
        self.fail_delete = False
        self.scope = SCOPE
        self.issued = 0
        self.admin_tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.token_forms: list[dict] = []
        self.storefront_tokens: set[str] = set()
        self.mints = 0
        self.deny_mint = False
        self.primary_host = "shop.acme.com"
        self.storefront_calls = 0
        self.checkout_url = f"https://{host}/cart/c/abc?key=k"
        self.cart_errors: list[dict] = []
        self.lines: list[dict] = []

    def _grant(self) -> dict:
        self.issued += 1
        access = f"{self.prefix}shpat-{self.issued}"
        refresh = f"{self.prefix}shprt-{self.issued}"
        self.admin_tokens.add(access)
        self.refresh_tokens.add(refresh)
        return {
            "access_token": access,
            "scope": self.scope,
            "expires_in": 3600,
            "refresh_token": refresh,
            "refresh_token_expires_in": 7776000,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == self.host, request.url
        path = request.url.path
        if path == "/admin/oauth/access_token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            assert form["client_id"] == CLIENT_ID and form["client_secret"] == SECRET
            if form.get("grant_type") == "refresh_token":
                token = form["refresh_token"]
                if token not in self.refresh_tokens:
                    return httpx.Response(
                        401,
                        json={
                            "error": "invalid_request",
                            "error_description": "This request requires an active refresh_token",
                        },
                    )
                self.refresh_tokens.discard(token)  # rotation: single use
                return httpx.Response(200, json=self._grant())
            if form.get("code") != "good-code":
                return httpx.Response(400, json={"error": "invalid_request"})
            return httpx.Response(200, json=self._grant())
        body = json.loads(request.content)
        query = body["query"]
        if path == API:
            if request.headers.get("x-shopify-access-token") not in self.admin_tokens:
                return httpx.Response(401, json={"errors": "Invalid API key or access token"})
            if "storefrontAccessTokenDelete" in query:
                if self.fail_delete:
                    return httpx.Response(500, json={})
                self.deleted.append(body["variables"]["input"]["id"])
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "storefrontAccessTokenDelete": {
                                "deletedStorefrontAccessTokenId": self.deleted[-1],
                                "userErrors": [],
                            }
                        }
                    },
                )
            if "storefrontAccessTokenCreate" in query:
                if self.deny_mint:
                    return httpx.Response(
                        200,
                        json={
                            "errors": [
                                {
                                    "message": "Access denied",
                                    "extensions": {"code": "ACCESS_DENIED"},
                                }
                            ]
                        },
                    )
                self.mints += 1
                token = f"{self.prefix}sf-token-{self.mints}"
                self.storefront_tokens.add(token)
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "storefrontAccessTokenCreate": {
                                "storefrontAccessToken": {
                                    "id": f"gid://shopify/StorefrontAccessToken/{token}",
                                    "accessToken": token,
                                },
                                "userErrors": [],
                            }
                        }
                    },
                )
            shop = {"name": "Acme Store", "myshopifyDomain": self.host}
            shop["primaryDomain"] = {"host": self.primary_host}
            return httpx.Response(200, json={"data": {"shop": shop}})
        assert path == STOREFRONT, path
        self.storefront_calls += 1
        if request.headers.get("x-shopify-storefront-access-token") not in self.storefront_tokens:
            return httpx.Response(401, json={})
        if "cartCreate" in query:
            self.lines.extend(body["variables"]["lines"])
            cart = None if self.cart_errors else {"checkoutUrl": self.checkout_url}
            return httpx.Response(
                200,
                json={"data": {"cartCreate": {"cart": cart, "userErrors": self.cart_errors}}},
            )
        if "primaryDomain" in query:
            return httpx.Response(
                200, json={"data": {"shop": {"primaryDomain": {"host": self.primary_host}}}}
            )
        first, after = body["variables"]["first"], body["variables"].get("after")
        start = int(after or 0)
        nodes = [_product_node(i) for i in range(start, min(start + first, 3))]
        return httpx.Response(
            200,
            json={
                "data": {
                    "products": {
                        "pageInfo": {
                            "hasNextPage": start + first < 3,
                            "endCursor": str(start + len(nodes)),
                        },
                        "nodes": nodes,
                    }
                }
            },
        )


def _product_node(i: int) -> dict:
    return {
        "id": f"gid://shopify/Product/{i}",
        "title": f"Mug {i}",
        "handle": f"mug-{i}",
        "featuredImage": {"url": f"https://cdn.shopify.com/mug-{i}.png"} if i else None,
        "priceRange": {"minVariantPrice": {"amount": "12.5", "currencyCode": "USD"}},
        "variants": {
            "nodes": [
                {
                    "id": f"gid://shopify/ProductVariant/{i}1",
                    "title": "Small",
                    "availableForSale": True,
                    "price": {"amount": "12.50", "currencyCode": "USD"},
                },
                {
                    "id": f"gid://shopify/ProductVariant/{i}2",
                    "title": "Large",
                    "availableForSale": False,
                    "price": {"amount": "15.00", "currencyCode": "USD"},
                },
            ]
        },
    }


@pytest.fixture
def fleet(monkeypatch):
    """Two shops behind one transport, routed by host."""
    from pocketpaw_ee.integrations.providers import oauth_http, shopify

    shops = {SHOP: FakeShop(SHOP), OTHER: FakeShop(OTHER)}
    transport = httpx.MockTransport(lambda r: shops[r.url.host].handle(r))
    monkeypatch.setattr(oauth_http, "_transport", transport)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_SHOPIFY_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_SHOPIFY_CLIENT_SECRET", SECRET)
    shopify.clear_cache()
    yield shops
    shopify.clear_cache()


@pytest.fixture
def shop(fleet):
    return fleet[SHOP]


def _esc(value: str, *, key: bool = False) -> str:
    value = value.replace("%", "%25").replace("&", "%26")
    return value.replace("=", "%3D") if key else value


def _sign(query: dict[str, str], secret: str = SECRET) -> str:
    """Shopify's reference signing: escaped keys and values, sorted, hex."""
    message = "&".join(f"{_esc(k, key=True)}={_esc(v)}" for k, v in sorted(query.items()))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


async def _authorize(c, pid: str | None, shop: str = SHOP) -> dict:
    """connect -> /start; returns the Shopify authorize URL's query."""
    body = {"params": {"shop": shop}}
    if pid:
        body["pocket_id"] = pid
    started = await c.post("/api/v1/integrations/shopify/connect", json=body)
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    location = urlsplit(start.headers["location"])
    assert location.scheme == "https" and location.netloc == shop
    assert location.path == "/admin/oauth/authorize"
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def _callback(c, state: str, *, shop: str | None = SHOP, tamper: bool = False) -> dict:
    query = {
        "code": "good-code",
        "host": base64.b64encode(f"{shop}/admin".encode()).decode(),
        "state": state,
        "timestamp": "1760400000",
    }
    if shop is not None:
        query["shop"] = shop
    signature = _sign(query)
    if tamper:
        query["code"] = "other-code"
    cb = await c.get("/api/v1/integrations/shopify/callback", params={**query, "hmac": signature})
    assert cb.status_code == 200
    return _message(cb.text)


async def _shop_site(c, shop: str = SHOP):
    pid = await _pocket()
    site = await _site(pid)
    q = await _authorize(c, pid, shop)
    msg = await _callback(c, q["state"], shop=shop)
    assert msg["status"] == "active" and "error" not in msg, msg
    return pid, str(site.id)


async def _binding(pid: str):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration

    return await SiteIntegration.find_one(
        SiteIntegration.pocket_id == pid, SiteIntegration.provider == "shopify"
    )


async def _conn(shop: str = SHOP):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection as Conn

    return await Conn.find_one(Conn.provider == "shopify", Conn.account_ref == shop)


# ------------------------------------------------------------------ connect


def test_shop_validation_and_unconfigured(monkeypatch, shop):
    from pocketpaw_ee.integrations.providers.shopify import ShopifyProvider

    provider = ShopifyProvider()
    assert provider.availability() == "available"
    assert provider.check_connect_params({"shop": " Acme-Store.MyShopify.com "}) == {"shop": SHOP}
    for bad in (
        "acme.myshopify.com.evil.io",
        "https://acme.myshopify.com",
        "-acme.myshopify.com",
        "acme.myshopify.com/admin",
        "acme_store.myshopify.com",
        "evil.com",
        "",
    ):
        with pytest.raises(ValueError):
            provider.check_connect_params({"shop": bad})
    with pytest.raises(ValueError):
        provider.check_connect_params({"shop": SHOP, "extra": "x"})
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_SHOPIFY_CLIENT_SECRET")
    assert provider.availability() == "unconfigured"


@pytest.mark.asyncio
async def test_connect_route_checks_params_and_the_catalog_declares_them(beanie_test_db, shop):
    async with _client() as c:
        [view] = [
            p
            for p in (await c.get("/api/v1/integrations/providers")).json()
            if p["id"] == "shopify"
        ]
        bad = await c.post(
            "/api/v1/integrations/shopify/connect", json={"params": {"shop": "evil.com"}}
        )
        missing = await c.post("/api/v1/integrations/shopify/connect", json={})
        extra = await c.post("/api/v1/integrations/demo/connect", json={"params": {"shop": SHOP}})
        q = await _authorize(c, None)
    assert view["kind"] == "connect" and view["category"] == "commerce" and view["plan"] == "site"
    assert view["connect_params"] == [
        {
            "key": "shop",
            "label": "Shopify store address",
            "placeholder": "your-store.myshopify.com",
        }
    ]
    assert [f["key"] for f in view["settings_fields"]] == ["shop_domain"]
    assert {a["name"] for a in view["actions"]} == {"list_products", "create_cart_checkout"}
    assert bad.status_code == missing.status_code == extra.status_code == 422
    assert q["client_id"] == CLIENT_ID and q["scope"] == SCOPE
    assert q["redirect_uri"] == "https://connect.example.com/api/v1/integrations/shopify/callback"
    assert q["state"]


@pytest.mark.asyncio
async def test_connect_mints_a_storefront_token_and_hides_it(beanie_test_db, shop):
    async with _client() as c:
        pid, _ = await _shop_site(c)
        [view] = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
        conns = (await c.get("/api/v1/integrations/connections")).json()
    conn = await _conn()
    assert conn.account_ref == SHOP and conn.account_label == "Acme Store"
    assert conn.meta["connect_params"] == {"shop": SHOP}
    assert set(conn.scopes) == set(SCOPE.split(","))
    assert conn.expires_at is not None
    [exchange] = shop.token_forms
    assert exchange["expiring"] == "1" and exchange["code"] == "good-code"
    assert view["status"] == "connected"
    assert view["settings"] == {"shop_domain": f"https://{SHOP}"}
    assert shop.mints == 1
    binding = await _binding(pid)
    assert binding.secret_ciphertext and "sf-token" not in binding.secret_ciphertext
    assert binding.settings["checkout_hosts"] == "shop.acme.com"
    dumped = json.dumps([view, conns])
    assert "sf-token" not in dumped and "shpat" not in dumped and "shprt" not in dumped

    # Setup again (a reconnect re-runs it): the token is kept, nothing re-minted.
    from pocketpaw_ee.integrations import bindings
    from pocketpaw_ee.integrations.providers import get_provider

    await bindings.run_setup(binding, get_provider("shopify"), conn, "")
    assert shop.mints == 1


@pytest.mark.asyncio
async def test_callback_with_a_bad_hmac_or_another_shop_is_refused(beanie_test_db, shop):
    async with _client() as c:
        q = await _authorize(c, None)
        tampered = await _callback(c, q["state"], tamper=True)
        q = await _authorize(c, None)
        other = await _callback(c, q["state"], shop="other-store.myshopify.com")
    assert tampered["error"] == "invalid_callback" and other["error"] == "invalid_callback"
    assert shop.token_forms == []  # the code was never spent
    assert await _conn() is None


def test_verify_callback_refuses_repeated_keys_and_a_missing_hmac(shop):
    from pocketpaw_ee.integrations.providers.shopify import ShopifyProvider

    provider = ShopifyProvider()
    query = {"code": "c", "shop": SHOP, "state": "s", "timestamp": "1"}
    signed = [*query.items(), ("hmac", _sign(query))]
    assert provider.verify_callback(signed, {"shop": SHOP}) is True
    assert provider.verify_callback([*signed, ("shop", SHOP)], {"shop": SHOP}) is False
    assert provider.verify_callback(list(query.items()), {"shop": SHOP}) is False
    assert provider.verify_callback(signed, {}) is False
    wrong = [*query.items(), ("hmac", _sign(query, "not-the-secret"))]
    assert provider.verify_callback(wrong, {"shop": SHOP}) is False


@pytest.mark.asyncio
async def test_refresh_rotates_and_a_dead_refresh_needs_reconnect(beanie_test_db, shop):
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.integrations import vault

    async with _client() as c:
        pid, _ = await _shop_site(c)
    conn = await _conn()
    for expected in ("shpat-2", "shpat-3"):
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await conn.save()
        assert await vault.get_valid_token(conn) == expected
        conn = await _conn()
    assert crypto.decrypt_json(conn.token_ciphertext)["refresh_token"] == "shprt-3"
    refreshes = [f for f in shop.token_forms if f.get("grant_type") == "refresh_token"]
    assert [f["refresh_token"] for f in refreshes] == ["shprt-1", "shprt-2"]

    # Shopify retires the refresh token (401 invalid_request): reconnect needed.
    shop.refresh_tokens.clear()
    conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await conn.save()
    with pytest.raises(vault.NotConnected):
        await vault.get_valid_token(conn)
    assert (await _conn()).status == "needs_reconnect"
    async with _client() as c:
        [view] = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
        resumed = await c.post(f"/api/v1/integrations/connections/{conn.id}/resume")
    assert view["status"] == "needs_reconnect"
    assert resumed.status_code == 200, resumed.text


@pytest.mark.asyncio
async def test_storefront_access_denied_is_setup_required(beanie_test_db, shop):
    shop.deny_mint = True
    async with _client() as c:
        pid, sid = await _shop_site(c)
        [view] = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
        listed = await _gw(c, sid, "shopify", "list_products", args={})
    binding = await _binding(pid)
    assert view["status"] == "setup_required"
    assert binding.setup_error == "storefront_access_denied" and not binding.secret_ciphertext
    assert listed.status_code == 409 and _code(listed) == "setup_required"


# ------------------------------------------------------------------ actions


@pytest.mark.asyncio
async def test_list_products_maps_prices_pages_caps_and_caches(beanie_test_db, shop):
    async with _client() as c:
        _, sid = await _shop_site(c)
        first = await _gw(c, sid, "shopify", "list_products", args={"limit": 2})
        calls = shop.storefront_calls
        cached = await _gw(c, sid, "shopify", "list_products", args={"limit": 2})
        assert shop.storefront_calls == calls
        second = await _gw(
            c,
            sid,
            "shopify",
            "list_products",
            args={"limit": 2, "cursor": first.json()["result"]["next_cursor"]},
        )
        too_many = await _gw(c, sid, "shopify", "list_products", args={"limit": 51})
        zero = await _gw(c, sid, "shopify", "list_products", args={"limit": 0})
        price = await _gw(c, sid, "shopify", "list_products", args={"price": 1})
    result = first.json()["result"]
    assert cached.json() == first.json()
    assert result["next_cursor"] == "2"
    assert result["products"][0] == {
        "id": "gid://shopify/Product/0",
        "title": "Mug 0",
        "handle": "mug-0",
        "image_url": None,
        "price_minor": 1250,
        "currency": "USD",
        "variants": [
            {
                "id": "gid://shopify/ProductVariant/01",
                "title": "Small",
                "price_minor": 1250,
                "available": True,
            },
            {
                "id": "gid://shopify/ProductVariant/02",
                "title": "Large",
                "price_minor": 1500,
                "available": False,
            },
        ],
    }
    assert result["products"][1]["image_url"] == "https://cdn.shopify.com/mug-1.png"
    page2 = second.json()["result"]
    assert [p["handle"] for p in page2["products"]] == ["mug-2"] and page2["next_cursor"] is None
    for resp in (too_many, zero, price):
        assert resp.status_code == 422 and _code(resp) == "invalid_args"


@pytest.mark.asyncio
async def test_cart_checkout_returns_only_a_shop_url(beanie_test_db, shop):
    async with _client() as c:
        _, sid = await _shop_site(c)

        async def _buy(**args):
            return await _gw(c, sid, "shopify", "create_cart_checkout", args=args)

        ok = await _buy(variant_id="gid://shopify/ProductVariant/11", qty=2)
        bare = await _buy(variant_id="12")
        shop.checkout_url = "https://shop.acme.com/checkouts/cn/x"
        custom = await _buy(variant_id="11")
        shop.checkout_url = "https://checkout.shopify.com/1/checkouts/x"
        hosted = await _buy(variant_id="11")
        shop.primary_host = "new.acme.com"
        shop.checkout_url = "https://new.acme.com/checkouts/cn/y"
        moved = await _buy(variant_id="11")
        bad = {}
        for name, url in {
            "evil": "https://evil.example/checkout",
            "http": f"http://{SHOP}/cart/c/abc",
            "creds": f"https://user@{SHOP}/cart",
            "port": f"https://{SHOP}:8443/cart",
            "lookalike": f"https://{SHOP}.evil.io/cart",
        }.items():
            shop.checkout_url = url
            bad[name] = await _buy(variant_id="11")
        shop.cart_errors = [{"code": "INVALID", "field": ["lines"], "message": "nope"}]
        refused = await _buy(variant_id="11")
        bad_args = [
            await _buy(variant_id="gid://shopify/Product/1"),
            await _buy(variant_id="11", qty=21),
            await _buy(variant_id="11", qty=0),
            await _buy(variant_id="11", price_minor=1),
        ]
    assert ok.json() == {"ok": True, "result": {"url": f"https://{SHOP}/cart/c/abc?key=k"}}
    assert bare.json()["ok"] is True
    assert shop.lines[:2] == [
        {"merchandiseId": "gid://shopify/ProductVariant/11", "quantity": 2},
        {"merchandiseId": "gid://shopify/ProductVariant/12", "quantity": 1},
    ]
    assert custom.json()["result"]["url"] == "https://shop.acme.com/checkouts/cn/x"
    assert hosted.json()["ok"] is True and moved.json()["ok"] is True
    for name, resp in bad.items():
        assert resp.status_code == 502 and _code(resp) == "provider_error", name
    assert refused.status_code == 422 and _code(refused) == "invalid_args"
    for resp in bad_args:
        assert resp.status_code == 422 and _code(resp) == "invalid_args"


# ----------------------------------------------------------------- webhooks


def _webhook(
    topic: str,
    payload: dict,
    *,
    secret: str = SECRET,
    wid: str = "wh-1",
    triggered_at: str | None = None,
):
    body = json.dumps(payload).encode()
    digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    headers = {
        "x-shopify-hmac-sha256": digest,
        "x-shopify-topic": topic,
        "x-shopify-shop-domain": SHOP,
        "x-shopify-webhook-id": wid,
        "content-type": "application/json",
    }
    if triggered_at is not None:
        headers["x-shopify-triggered-at"] = triggered_at
    return body, headers


URL = "/api/v1/integrations/webhooks/shopify"


@pytest.mark.asyncio
async def test_webhooks_need_the_app_signature_and_a_matching_body(beanie_test_db, shop):
    async with _client() as c:
        body, headers = _webhook("customers/redact", {"shop_domain": SHOP}, secret="wrong")
        forged = await c.post(URL, content=body, headers=headers)
        # A signed customers/redact body relabelled as shop/redact is refused.
        body, headers = _webhook(
            "shop/redact", {"shop_id": 1, "shop_domain": SHOP, "customer": {"id": 1}}
        )
        relabelled = await c.post(URL, content=body, headers=headers)
        body, headers = _webhook(
            "customers/data_request",
            {
                "shop_id": 1,
                "shop_domain": SHOP,
                "customer": {"id": 7, "email": "a@b.co"},
                "orders_requested": [],
                "data_request": {"id": 9},
            },
        )
        data_request = await c.post(URL, content=body, headers=headers)
        body, headers = _webhook(
            "customers/redact",
            {"shop_id": 1, "shop_domain": SHOP, "customer": {"id": 7}, "orders_to_redact": []},
            wid="wh-redact",
        )
        redact = await c.post(URL, content=body, headers=headers)
    assert forged.status_code == 401 and relabelled.status_code == 401
    assert data_request.status_code == 200 and data_request.json() == {"ok": True}
    assert redact.status_code == 200 and redact.json() == {"ok": True}


@pytest.mark.asyncio
async def test_uninstall_needs_reconnect_and_shop_redact_purges(beanie_test_db, shop):
    async with _client() as c:
        pid, sid = await _shop_site(c)
        body, headers = _webhook(
            "app/uninstalled", {"id": 1, "name": "Acme Store", "myshopify_domain": SHOP}
        )
        gone = await c.post(URL, content=body, headers=headers)
        [after_uninstall] = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
        [conn_view] = (await c.get("/api/v1/integrations/connections")).json()
        listed = await _gw(c, sid, "shopify", "list_products", args={})
        conn = await _conn()
        assert conn.status == "needs_reconnect" and conn.token_ciphertext == ""
        binding = await _binding(pid)
        assert binding.secret_ciphertext == "" and binding.connection_id == str(conn.id)

        body, headers = _webhook("shop/redact", {"shop_id": 1, "shop_domain": SHOP}, wid="wh-2")
        purged = await c.post(URL, content=body, headers=headers)
        [after_redact] = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    assert gone.status_code == 200 and purged.status_code == 200
    assert after_uninstall["status"] == "needs_reconnect"
    assert "removed from this Shopify store" in conn_view["attention"]
    assert listed.status_code == 409 and _code(listed) == "not_connected"
    assert await _conn() is None
    binding = await _binding(pid)
    assert binding.connection_id is None and binding.secret_ciphertext == ""
    assert "shop_domain" not in (binding.settings or {})
    assert after_redact["status"] == "needs_connection"


# ------------------------------------------------------- review hardening


def test_callback_hmac_uses_shopify_escaping_and_accepts_uppercase_hex(shop):
    from pocketpaw_ee.integrations.providers.shopify import ShopifyProvider

    provider = ShopifyProvider()
    query = {"code": "c", "shop": SHOP, "state": "s", "note": "a&b=c%d", "k=x&y": "v"}
    signed = [*query.items(), ("hmac", _sign(query).upper())]
    assert provider.verify_callback(signed, {"shop": SHOP}) is True
    # The naive (unescaped) message is not what Shopify signs.
    naive = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
    raw = hmac.new(SECRET.encode(), naive.encode(), hashlib.sha256).hexdigest()
    assert provider.verify_callback([*query.items(), ("hmac", raw)], {"shop": SHOP}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [None, "ACME-STORE.myshopify.com", f"{SHOP}.", OTHER])
async def test_callback_shop_must_be_exactly_the_states(beanie_test_db, shop, variant):
    async with _client() as c:
        q = await _authorize(c, None)
        msg = await _callback(c, q["state"], shop=variant)
    assert msg["error"] == "invalid_callback"
    assert shop.token_forms == [] and await _conn() is None


def test_shop_regex_is_a_full_match(shop):
    from pocketpaw_ee.integrations.providers.shopify import SHOP_RE, valid_shop

    assert SHOP_RE.fullmatch(f"{SHOP}\n") is None
    with pytest.raises(ValueError):
        valid_shop(f"{SHOP}\nx")


@pytest.mark.asyncio
async def test_refresh_and_resume_take_the_shop_from_meta(beanie_test_db, fleet):
    from pocketpaw_ee.integrations import vault

    async with _client() as c:
        await _shop_site(c, OTHER)
        conn = await _conn(OTHER)
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await conn.save()
        assert await vault.get_valid_token(conn) == "b-shpat-2"
        [refresh] = [f for f in fleet[OTHER].token_forms if f.get("grant_type")]
        assert refresh["refresh_token"] == "b-shprt-1"
        assert fleet[SHOP].token_forms == []

        conn = await _conn(OTHER)
        conn.status = "needs_reconnect"
        await conn.save()
        resumed = await c.post(f"/api/v1/integrations/connections/{conn.id}/resume")
        split = urlsplit(resumed.json()["authorize_url"])
        start = await c.get(split.path, params=parse_qs(split.query))
    assert urlsplit(start.headers["location"]).netloc == OTHER


@pytest.mark.asyncio
async def test_another_shops_uninstall_and_redact_leave_this_shop_alone(beanie_test_db, fleet):
    async with _client() as c:
        pid, _ = await _shop_site(c)
        for i, (topic, payload) in enumerate(
            [
                ("app/uninstalled", {"id": 2, "myshopify_domain": OTHER}),
                ("shop/redact", {"shop_id": 2, "shop_domain": OTHER}),
            ]
        ):
            body, headers = _webhook(topic, payload, wid=f"other-{i}")
            assert (await c.post(URL, content=body, headers=headers)).status_code == 200
    conn = await _conn()
    binding = await _binding(pid)
    assert conn.status == "active" and conn.token_ciphertext
    assert binding.secret_ciphertext and binding.connection_id == str(conn.id)


@pytest.mark.asyncio
async def test_a_late_uninstall_after_a_reconnect_is_ignored(beanie_test_db, shop):
    async with _client() as c:
        pid, _ = await _shop_site(c)
        before = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
        body, headers = _webhook(
            "app/uninstalled", {"id": 1, "myshopify_domain": SHOP}, triggered_at=before
        )
        late = await c.post(URL, content=body, headers=headers)
        assert (await _conn()).status == "active" and (await _binding(pid)).secret_ciphertext
        after = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        body, headers = _webhook(
            "app/uninstalled", {"id": 1, "myshopify_domain": SHOP}, wid="wh-9", triggered_at=after
        )
        real = await c.post(URL, content=body, headers=headers)
    assert late.status_code == 200 and real.status_code == 200
    assert (await _conn()).status == "needs_reconnect"


@pytest.mark.asyncio
async def test_shop_redact_never_purges_an_active_connection(beanie_test_db, shop):
    async with _client() as c:
        pid, _ = await _shop_site(c)
        body, headers = _webhook("shop/redact", {"shop_id": 1, "shop_domain": SHOP})
        resp = await c.post(URL, content=body, headers=headers)
    assert resp.status_code == 200
    conn = await _conn()
    assert conn is not None and conn.status == "active"
    assert (await _binding(pid)).connection_id == str(conn.id)


@pytest.mark.asyncio
async def test_a_repeated_webhook_id_is_acked_as_a_duplicate(beanie_test_db, shop):
    async with _client() as c:
        await _shop_site(c)
        body, headers = _webhook("app/uninstalled", {"id": 1, "myshopify_domain": SHOP}, wid="dup")
        first = await c.post(URL, content=body, headers=headers)
        # Reconnect, then Shopify redelivers the same webhook id.
        conn = await _conn()
        resumed = await c.post(f"/api/v1/integrations/connections/{conn.id}/resume")
        split = urlsplit(resumed.json()["authorize_url"])
        start = await c.get(split.path, params=parse_qs(split.query))
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        assert (await _callback(c, state))["status"] == "active"
        again = await c.post(URL, content=body, headers=headers)
    assert first.json() == {"ok": True}
    assert again.json() == {"ok": True, "duplicate": True}
    assert (await _conn()).status == "active"


@pytest.mark.asyncio
async def test_hidden_settings_cannot_be_set_by_an_editor(beanie_test_db, shop):
    async with _client() as c:
        pid, _ = await _shop_site(c)
        before = dict((await _binding(pid)).settings)
        await c.put(
            f"/api/v1/sites/by-pocket/{pid}/integrations/shopify",
            json={
                "settings": {
                    "storefront_shop": "evil.myshopify.com",
                    "checkout_hosts": "evil.example",
                    "storefront_token_id": "gid://x",
                    "shop_domain": "https://evil.example",
                }
            },
        )
    assert (await _binding(pid)).settings == before


@pytest.mark.asyncio
async def test_the_product_cache_is_per_site(beanie_test_db, shop):
    from pocketpaw_ee.integrations.providers import shopify
    from pocketpaw_ee.integrations.providers.base import ActionContext

    pids = [await _pocket(), await _pocket()]
    async with _client() as c:
        for pid in pids:
            q = await _authorize(c, pid)
            assert (await _callback(c, q["state"]))["status"] == "active"

    async def _list(pid: str):
        binding = await _binding(pid)
        ctx = ActionContext(
            workspace_id=binding.workspace,
            pocket_id=pid,
            site_id="s",
            provider="shopify",
            action="list_products",
            settings=dict(binding.settings),
            caller="site_key",
        )
        return await shopify._list_products(ctx, shopify.ListProductsArgs(limit=1))

    await _list(pids[0])
    calls = shop.storefront_calls
    await _list(pids[0])
    assert shop.storefront_calls == calls
    await _list(pids[1])
    assert shop.storefront_calls == calls + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_delete", [False, True])
async def test_a_shop_change_drops_the_old_token_even_when_the_new_mint_fails(
    beanie_test_db, fleet, fail_delete
):
    fleet[SHOP].fail_delete = fail_delete
    fleet[OTHER].deny_mint = True
    async with _client() as c:
        pid, sid = await _shop_site(c)
        old_id = (await _binding(pid)).settings["storefront_token_id"]
        q = await _authorize(c, pid, OTHER)
        assert (await _callback(c, q["state"], shop=OTHER))["status"] == "active"
        listed = await _gw(c, sid, "shopify", "list_products", args={})
    binding = await _binding(pid)
    assert binding.connection_id == str((await _conn(OTHER)).id)
    assert binding.secret_ciphertext == "" and not binding.settings.get("storefront_shop")
    assert not binding.settings.get("storefront_token_id")
    assert binding.setup_error == "storefront_access_denied"
    assert fleet[SHOP].deleted == ([] if fail_delete else [old_id])
    assert listed.status_code == 409 and _code(listed) == "setup_required"


@pytest.mark.asyncio
async def test_unlinking_deletes_the_storefront_token(beanie_test_db, shop):
    async with _client() as c:
        pid, _ = await _shop_site(c)
        token_id = (await _binding(pid)).settings["storefront_token_id"]
        gone = await c.delete(f"/api/v1/sites/by-pocket/{pid}/integrations/shopify")
    assert gone.status_code == 204
    assert shop.deleted == [token_id]
