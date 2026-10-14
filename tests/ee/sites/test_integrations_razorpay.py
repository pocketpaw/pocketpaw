# tests/ee/sites/test_integrations_razorpay.py: the ``razorpay`` Sites integration.
#
# Razorpay's OAuth token endpoint and Payment Links API are an in-memory fake
# (``FakeRazorpay``) swapped in as ``oauth_http._transport``. Covers the partner
# OAuth flow (authorize params, JSON code exchange, account id as account_ref,
# rotating refresh persisted by the vault, a refused refresh -> InvalidGrant),
# ``create_checkout`` (catalog pricing, currency, qty / minimum bounds, safe
# paths, 400 / 401 handling) and the webhook (signature, paid / refunded / failed,
# dedupe, the merchant's own links ignored, cross-account refusal). No network.
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    OWNER,
    WS,
    _client,
    _code,
    _env,
    _gw,
    _message,
    _pocket,
    _site,
)

CLIENT_ID = "rzp-client"
CLIENT_SECRET = "rzp-secret"
WHSEC = "rzp-webhook-secret"
ACCOUNT = "acc_Merchant0001"
SHOP = "https://shop.example.com"
LINKS = "/api/v1/integrations/webhooks/razorpay"


class FakeRazorpay:
    """auth.razorpay.com/token + api.razorpay.com/v1/payment_links."""

    def __init__(self) -> None:
        self.token_bodies: list[dict] = []
        self.issued = 0
        self.live_access: set[str] = set()
        self.live_refresh: set[str] = set()
        self.links: list[dict] = []
        self.auth_headers: list[str] = []
        self.link_status: int | None = None
        self.account = ACCOUNT

    def _pair(self, *, with_account: bool) -> dict:
        self.issued += 1
        access, refresh = f"rzp-at-{self.issued}", f"rzp-rt-{self.issued}"
        self.live_access.add(access)
        self.live_refresh.add(refresh)
        body = {
            "public_token": f"rzp_test_oauth_{self.issued}",
            "token_type": "Bearer",
            "expires_in": 7862400,
            "access_token": access,
            "refresh_token": refresh,
        }
        if with_account:
            body["razorpay_account_id"] = self.account
        return body

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "auth.razorpay.com" and url.path == "/token":
            assert request.headers["content-type"] == "application/json"
            body = json.loads(request.content)
            self.token_bodies.append(body)
            if body.get("client_secret") != CLIENT_SECRET:
                return httpx.Response(401, json={"error": {"description": "Access denied"}})
            if body["grant_type"] == "authorization_code":
                if body.get("code") != "good-code":
                    desc = "Authorization code has expired"
                    return httpx.Response(400, json={"error": {"description": desc}})
                return httpx.Response(200, json=self._pair(with_account=True))
            if body["grant_type"] == "refresh_token":
                old = body.get("refresh_token")
                if old not in self.live_refresh:
                    desc = "Token has been revoked"
                    return httpx.Response(400, json={"error": {"description": desc}})
                self.live_refresh.discard(old)  # rotating: the old one dies
                return httpx.Response(200, json=self._pair(with_account=False))
        if url.host == "api.razorpay.com" and url.path == "/v1/payment_links":
            auth = request.headers.get("authorization", "")
            self.auth_headers.append(auth)
            if auth.removeprefix("Bearer ") not in self.live_access:
                return httpx.Response(401, json={"error": {"code": "BAD_REQUEST_ERROR"}})
            if self.link_status is not None:
                return httpx.Response(self.link_status, json={"error": {"code": "X"}})
            self.links.append(json.loads(request.content))
            n = len(self.links)
            return httpx.Response(
                200,
                json={
                    "id": f"plink_{n}",
                    "short_url": f"https://rzp.io/i/l{n}",
                    "status": "created",
                },
            )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})


@pytest.fixture
def rzp(monkeypatch):
    from pocketpaw_ee.integrations.providers import oauth_http

    fake = FakeRazorpay()
    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(fake.handle))
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_RAZORPAY_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_RAZORPAY_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_RAZORPAY_WEBHOOK_SECRET", WHSEC)
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_RAZORPAY_MODE", raising=False)
    return fake


@pytest.fixture
async def catalog(tmp_path, monkeypatch):
    """A Paw Bar store; returns an adder of one widget + items per pocket."""
    from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget
    from pocketpaw.paw_bar.store import PawBarStore

    store = PawBarStore(tmp_path / "pb.db")
    monkeypatch.setattr("pocketpaw.stores.get_paw_bar_store", lambda: store)

    async def _add(pocket_id: str, *items: dict, workspace: str = WS) -> None:
        widget = await store.create_widget(
            PawBarWidget(
                pocket_id=pocket_id,
                owner=OWNER,
                workspace_id=workspace,
                spec=PawBarSpec(widget_id="w", pocket_id=pocket_id),
            )
        )
        await store.upsert_catalog_items(widget.id, list(items), workspace_id=workspace)

    return _add


async def _connect(c, pid: str | None = None, *, code: str = "good-code") -> dict:
    body = {"pocket_id": pid} if pid else {}
    started = await c.post("/api/v1/integrations/razorpay/connect", json=body)
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    start = await c.get(split.path, params=parse_qs(split.query))
    assert start.status_code == 302, start.text
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    cb = await c.get(
        "/api/v1/integrations/razorpay/callback", params={"code": code, "state": query["state"]}
    )
    assert cb.status_code == 200
    return _message(cb.text)


async def _ready_site(c, catalog, *, settings: dict | None = None):
    pid = await _pocket()
    site = await _site(pid)
    site.url = SHOP + "/"
    await site.save()
    await catalog(
        pid,
        {"id": "kurta", "name": "Cotton Kurta", "price_cents": 125000, "currency": "INR"},
        {"id": "free", "name": "Sticker", "price_cents": 0, "currency": "INR"},
        {"id": "gone", "name": "Old", "price_cents": 900, "currency": "INR", "in_stock": False},
        {"id": "usd", "name": "Mug", "price_cents": 1500, "currency": "USD"},
        {"id": "tiny", "name": "Pin", "price_cents": 50, "currency": "INR"},
    )
    msg = await _connect(c, pid)
    assert msg["status"] == "active", msg
    body = {"enabled_actions": ["create_checkout"], "settings": settings or {}}
    put = await c.put(f"/api/v1/sites/by-pocket/{pid}/integrations/razorpay", json=body)
    assert put.status_code == 200, put.text
    return pid, str(site.id), msg["connection_id"]


async def _conn(conn_id: str):
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return await IntegrationConnection.get(PydanticObjectId(conn_id))


# ------------------------------------------------------------------ catalog + oauth


@pytest.mark.asyncio
async def test_catalog_view_and_unconfigured_without_client_creds(beanie_test_db, rzp, monkeypatch):
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_RAZORPAY_CLIENT_SECRET")
    async with _client() as c:
        views = {p["id"]: p for p in (await c.get("/api/v1/integrations/providers")).json()}
        refused = await c.post("/api/v1/integrations/razorpay/connect", json={})
    view = views["razorpay"]
    assert (view["kind"], view["category"], view["availability"]) == (
        "connect",
        "payments",
        "unconfigured",
    )
    assert [f["key"] for f in view["settings_fields"]] == ["currency", "success_path"]
    assert [a["name"] for a in view["actions"]] == ["create_checkout"]
    assert refused.status_code == 409


def test_authorize_url_params(rzp):
    from pocketpaw_ee.integrations.providers.razorpay import CURRENCIES, RazorpayProvider

    provider = RazorpayProvider()
    assert provider.availability() == "available" and provider.supports_pkce is False
    split = urlsplit(provider.authorize_url("st-1", "https://connect.example.com/cb"))
    assert f"{split.scheme}://{split.netloc}{split.path}" == "https://auth.razorpay.com/authorize"
    assert {k: v[0] for k, v in parse_qs(split.query).items()} == {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": "https://connect.example.com/cb",
        "scope": "read_write",
        "state": "st-1",
    }
    assert CURRENCIES[0] == "INR" and "KWD" not in CURRENCIES


@pytest.mark.asyncio
async def test_connect_exchanges_the_code_and_keys_on_the_account(beanie_test_db, rzp):
    from pocketpaw_ee.cloud._core import crypto

    pid = await _pocket()
    async with _client() as c:
        msg = await _connect(c, pid)
        bad = await _connect(c, code="stale-code")
        listed = (await c.get("/api/v1/integrations/connections")).json()
    assert msg["status"] == "active", msg
    assert bad["status"] == "error"
    exchange = rzp.token_bodies[0]
    assert exchange == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": "https://connect.example.com/api/v1/integrations/razorpay/callback",
        "code": "good-code",
    }
    conn = await _conn(msg["connection_id"])
    assert conn.account_ref == ACCOUNT and conn.account_label == f"Razorpay {ACCOUNT}"
    tokens = crypto.decrypt_json(conn.token_ciphertext)
    assert (tokens["access_token"], tokens["refresh_token"]) == ("rzp-at-1", "rzp-rt-1")
    assert conn.expires_at is not None
    assert len(listed) == 1 and "rzp-at" not in json.dumps(listed)


@pytest.mark.asyncio
async def test_test_mode_env_sends_mode_test(beanie_test_db, rzp, monkeypatch):
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_RAZORPAY_MODE", "test")
    async with _client() as c:
        msg = await _connect(c)
    assert msg["status"] == "active" and rzp.token_bodies[0]["mode"] == "test"


@pytest.mark.asyncio
async def test_refresh_rotates_and_a_dead_refresh_token_is_invalid_grant(rzp):
    from pocketpaw_ee.integrations.providers.base import InvalidGrant
    from pocketpaw_ee.integrations.providers.razorpay import RazorpayProvider

    rzp.live_refresh.add("rt-0")
    grant = await RazorpayProvider().refresh("rt-0")
    assert (grant.access_token, grant.refresh_token, grant.expires_in) == (
        "rzp-at-1",
        "rzp-rt-1",
        7862400,
    )
    assert rzp.token_bodies[-1] == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": "rt-0",
    }
    with pytest.raises(InvalidGrant):
        await RazorpayProvider().refresh("rt-0")  # rotated away


@pytest.mark.asyncio
async def test_vault_refresh_persists_the_rotated_refresh_token(beanie_test_db, rzp, catalog):
    from pocketpaw_ee.cloud._core import crypto

    async with _client() as c:
        _, sid, conn_id = await _ready_site(c, catalog)
        conn = await _conn(conn_id)
        conn.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await conn.save()
        resp = await _gw(c, sid, "razorpay", "create_checkout", args={"product_ref": "kurta"})
    assert resp.status_code == 200, resp.text
    row = await _conn(conn_id)
    tokens = crypto.decrypt_json(row.token_ciphertext)
    assert (tokens["access_token"], tokens["refresh_token"]) == ("rzp-at-2", "rzp-rt-2")
    assert row.account_ref == ACCOUNT
    assert rzp.auth_headers[-1] == "Bearer rzp-at-2"


# ------------------------------------------------------------------ checkout


@pytest.mark.asyncio
async def test_checkout_prices_from_the_catalog_never_the_client(beanie_test_db, rzp, catalog):
    async with _client() as c:
        pid, sid, _ = await _ready_site(c, catalog, settings={"success_path": "/thanks"})
        ok = await _gw(
            c, sid, "razorpay", "create_checkout", args={"product_ref": "kurta", "qty": 3}
        )
        sneaky = await _gw(
            c, sid, "razorpay", "create_checkout", args={"product_ref": "kurta", "amount": 1}
        )
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"] == {"url": "https://rzp.io/i/l1"}
    assert _code(sneaky) == "invalid_args"
    [link] = rzp.links
    assert (link["amount"], link["currency"], link["accept_partial"]) == (375000, "INR", False)
    assert link["reference_id"].startswith("paw_") and len(link["reference_id"]) <= 40
    assert link["callback_url"] == f"{SHOP}/thanks" and link["callback_method"] == "get"
    assert link["notes"] == {
        "site_id": sid,
        "pocket_id": pid,
        "product_ref": "kurta",
        "product_name": "Cotton Kurta",
        "qty": "3",
        "unit_amount": "125000",
    }
    assert link["expire_by"] > datetime.now(UTC).timestamp() + 15 * 60
    assert rzp.auth_headers == ["Bearer rzp-at-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"product_ref": "nope", "qty": 1},
        {"product_ref": "free", "qty": 1},
        {"product_ref": "gone", "qty": 1},
        {"product_ref": "usd", "qty": 1},  # binding currency defaults to INR
        {"product_ref": "tiny", "qty": 1},  # 50 paise is below Razorpay's minimum
        {"product_ref": "kurta", "qty": 0},
        {"product_ref": "kurta", "qty": 100},
        {"product_ref": "x" * 121, "qty": 1},
    ],
)
async def test_checkout_refuses_unusable_products_and_bad_qty(beanie_test_db, rzp, catalog, args):
    async with _client() as c:
        _, sid, _ = await _ready_site(c, catalog)
        resp = await _gw(c, sid, "razorpay", "create_checkout", args=args)
    assert resp.status_code == 422
    assert _code(resp) == "invalid_args"
    assert rzp.links == []


@pytest.mark.asyncio
async def test_checkout_other_currency_custom_domain_and_unsafe_path(beanie_test_db, rzp, catalog):
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.integration import SiteIntegration
    from pocketpaw_ee.cloud.models.site import Site, SiteDomain

    async with _client() as c:
        pid, sid, _ = await _ready_site(c, catalog, settings={"currency": "USD"})
        site = await Site.get(PydanticObjectId(sid))
        site.domains = [SiteDomain(hostname="buy.example.org", status="live")]
        await site.save()
        # A stored path that isn't one (set behind the PUT's back) reads as "/".
        row = await SiteIntegration.find_one(SiteIntegration.pocket_id == pid)
        row.settings = {**row.settings, "success_path": "//evil.example/x"}
        await row.save()
        usd = await _gw(c, sid, "razorpay", "create_checkout", args={"product_ref": "usd"})
        inr = await _gw(c, sid, "razorpay", "create_checkout", args={"product_ref": "kurta"})
    assert usd.status_code == 200, usd.text
    assert _code(inr) == "invalid_args"
    [link] = rzp.links
    assert (link["amount"], link["currency"]) == (1500, "USD")
    assert link["callback_url"] == "https://buy.example.org/"


@pytest.mark.asyncio
async def test_razorpay_400_is_invalid_args_and_429_rate_limited(beanie_test_db, rzp, catalog):
    async with _client() as c:
        _, sid, _ = await _ready_site(c, catalog)
        rzp.link_status = 400
        bad = await _gw(c, sid, "razorpay", "create_checkout", args={"product_ref": "kurta"})
        rzp.link_status = 429
        limited = await _gw(c, sid, "razorpay", "create_checkout", args={"product_ref": "kurta"})
    assert _code(bad) == "invalid_args" and _code(limited) == "rate_limited"


@pytest.mark.asyncio
async def test_a_401_refreshes_once_and_retries(beanie_test_db, rzp, catalog):
    async with _client() as c:
        _, sid, _ = await _ready_site(c, catalog)
        rzp.live_access.clear()  # revoked server-side before its stated expiry
        resp = await _gw(c, sid, "razorpay", "create_checkout", args={"product_ref": "kurta"})
    assert resp.status_code == 200, resp.text
    assert rzp.auth_headers == ["Bearer rzp-at-1", "Bearer rzp-at-2"]


@pytest.mark.asyncio
async def test_settings_put_validates_currency_and_path_length(beanie_test_db, rzp):
    pid = await _pocket()
    url = f"/api/v1/sites/by-pocket/{pid}/integrations/razorpay"
    async with _client() as c:
        bad_currency = await c.put(url, json={"settings": {"currency": "KWD"}})
        long_path = await c.put(url, json={"settings": {"success_path": "/" + "a" * 200}})
        good = await c.put(url, json={"settings": {"currency": "INR", "success_path": "/ok"}})
    assert bad_currency.status_code == 422 and long_path.status_code == 422
    assert good.status_code == 200 and good.json()["settings"]["currency"] == "INR"


# ------------------------------------------------------------------ webhooks


def _signed(event: dict, *, eid: str, secret: str = WHSEC) -> tuple[bytes, dict]:
    from pocketpaw_ee.integrations.providers.razorpay import sign

    body = json.dumps(event).encode()
    return body, {
        "x-razorpay-signature": sign(body, secret),
        "x-razorpay-event-id": eid,
        "content-type": "application/json",
    }


async def _post(c, event: dict, *, eid: str, **kw):
    body, headers = _signed(event, eid=eid, **kw)
    return await c.post(LINKS, content=body, headers=headers)


def _payment(order_id: str, notes, **extra) -> dict:
    return {
        "id": "pay_1",
        "entity": "payment",
        "amount": 375000,
        "currency": "INR",
        "status": "captured",
        "order_id": order_id,
        "email": "buyer@x.in",
        "notes": notes,
        **extra,
    }


def _paid(sid: str, *, order_id: str = "order_A", acct: str = ACCOUNT, notes=None) -> dict:
    notes = (
        {"site_id": sid, "product_ref": "kurta", "product_name": "Cotton Kurta", "qty": "3"}
        if notes is None
        else notes
    )
    link = {
        "id": "plink_1",
        "amount": 375000,
        "amount_paid": 375000,
        "currency": "INR",
        "status": "paid",
        "order_id": order_id,
        "reference_id": "paw_abc",
        "customer": {"email": "buyer@x.in"},
        "notes": notes,
    }
    return {
        "entity": "event",
        "account_id": acct,
        "event": "payment_link.paid",
        "contains": ["payment_link", "order", "payment"],
        "payload": {
            "payment_link": {"entity": link},
            "payment": {"entity": _payment(order_id, notes)},
            "order": {"entity": {"id": order_id, "notes": notes}},
        },
        "created_at": int(time.time()),
    }


def _refund(order_id: str, *, refunded: int, status: str, acct: str = ACCOUNT) -> dict:
    payment = _payment(order_id, [], amount_refunded=refunded, refund_status=status)
    return {
        "entity": "event",
        "account_id": acct,
        "event": "refund.processed",
        "contains": ["refund", "payment"],
        "payload": {
            "refund": {"entity": {"id": "rfnd_1", "payment_id": "pay_1", "amount": refunded}},
            "payment": {"entity": payment},
        },
        "created_at": int(time.time()),
    }


def _failed(order_id: str, notes) -> dict:
    payment = _payment(order_id, notes, status="failed")
    return {
        "entity": "event",
        "account_id": ACCOUNT,
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": payment}},
        "created_at": int(time.time()),
    }


def test_signature_check():
    from pocketpaw_ee.integrations.providers.razorpay import sign, verify_signature

    body = b'{"event":"payment.failed"}'
    assert verify_signature(body, sign(body, WHSEC), WHSEC)
    assert not verify_signature(body, sign(body, "other"), WHSEC)
    assert not verify_signature(body + b" ", sign(body, WHSEC), WHSEC)
    assert not verify_signature(body, "", WHSEC)
    assert not verify_signature(body, sign(body, ""), "")


@pytest.mark.asyncio
async def test_webhook_signature_valid_invalid_and_ignored(beanie_test_db, rzp, monkeypatch):
    async with _client() as c:
        forged = await _post(c, {"event": "payment.captured"}, eid="e1", secret="nope")
        now = int(time.time())
        ignored = await _post(
            c, {"event": "payment.captured", "payload": {}, "created_at": now}, eid="e2"
        )
        stale = await _post(c, {"event": "payment.captured", "created_at": now - 90000}, eid="e4")
        undated = await _post(c, {"event": "payment.captured", "payload": {}}, eid="e5")
        monkeypatch.delenv("POCKETPAW_INTEGRATIONS_RAZORPAY_WEBHOOK_SECRET")
        unset = await _post(c, {"event": "payment.captured"}, eid="e3", secret="")
    assert forged.status_code == 401 and unset.status_code == 401
    assert stale.status_code == 401 and undated.status_code == 401
    assert ignored.status_code == 200 and ignored.json().get("ignored") is True


@pytest.mark.asyncio
async def test_orders_paid_refunded_failed_and_deduped(beanie_test_db, rzp, catalog):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    async with _client() as c:
        pid, sid, _ = await _ready_site(c, catalog)
        paid = await _post(c, _paid(sid), eid="evt_1")
        dup = await _post(c, _paid(sid), eid="evt_1")
        assert paid.json() == {"ok": True} and dup.json()["duplicate"] is True
        [order] = await SiteOrder.find_all().to_list()
        assert (order.external_id, order.status, order.amount_minor, order.currency) == (
            "order_A",
            "paid",
            375000,
            "INR",
        )
        assert order.customer_email == "buyer@x.in" and order.pocket_id == pid
        assert order.site_id == sid
        assert [(i.ref, i.name, i.qty) for i in order.items] == [("kurta", "Cotton Kurta", 3)]

        # A late failed attempt on the same order never undoes paid.
        await _post(c, _failed("order_A", {"site_id": sid}), eid="evt_2")
        assert (await SiteOrder.find_one()).status == "paid"
        # A partial refund leaves it paid; a full one (no notes on the payment)
        # routes by the order id and settles it.
        partial = await _post(c, _refund("order_A", refunded=100, status="partial"), eid="evt_3")
        assert partial.json()["ignored"] is True
        assert (await SiteOrder.find_one()).status == "paid"
        full = await _post(c, _refund("order_A", refunded=375000, status="full"), eid="evt_4")
        assert full.json() == {"ok": True}
        assert (await SiteOrder.find_one()).status == "refunded"
        # Refunded is terminal.
        await _post(c, _paid(sid), eid="evt_5")
        assert (await SiteOrder.find_one()).status == "refunded"

        # A failure or refund for an order we never recorded makes no row, and the
        # merchant's own payment links (no site id in notes) are not site orders.
        await _post(c, _failed("order_B", {"site_id": sid}), eid="evt_6")
        early = await _post(c, _refund("order_C", refunded=375000, status="full"), eid="evt_7")
        assert early.status_code == 500
        own = await _post(c, _paid(sid, order_id="order_D", notes=[]), eid="evt_8")
        assert own.json()["ignored"] is True
        orders = [o.external_id for o in await SiteOrder.find_all().to_list()]
    assert orders == ["order_A"]


@pytest.mark.asyncio
async def test_an_event_from_another_account_is_refused(beanie_test_db, rzp, catalog):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    async with _client() as c:
        _, sid, _ = await _ready_site(c, catalog)
        resp = await _post(c, _paid(sid, acct="acc_Stranger"), eid="evt_x")
        await _post(c, _paid(sid), eid="evt_y")
        refund = await _post(
            c, _refund("order_A", refunded=375000, status="full", acct="acc_Stranger"), eid="evt_z"
        )
        status = (await SiteOrder.find_one()).status
    assert resp.json() == {"ok": True, "routed": False}
    assert refund.json() == {"ok": True, "routed": False}
    assert status == "paid"


@pytest.mark.asyncio
async def test_event_id_is_bound_to_the_body(beanie_test_db, rzp, catalog):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    async with _client() as c:
        _, sid, _ = await _ready_site(c, catalog)
        first = await _post(c, _paid(sid, order_id="order_A"), eid="evt_same")
        # A forged/reused header on another (signed) body is not a duplicate of it.
        other = await _post(c, _paid(sid, order_id="order_B"), eid="evt_same")
        orders = sorted(o.external_id for o in await SiteOrder.find_all().to_list())
    assert first.json() == {"ok": True} and other.json() == {"ok": True}
    assert orders == ["order_A", "order_B"]


@pytest.mark.asyncio
async def test_an_early_refund_is_retried_and_applies_after_paid(beanie_test_db, rzp, catalog):
    from pocketpaw_ee.cloud.models.integration import IntegrationWebhookEvent, SiteOrder

    refund = _refund("order_A", refunded=375000, status="full")
    async with _client() as c:
        _, sid, _ = await _ready_site(c, catalog)
        early = await _post(c, refund, eid="evt_r")
        assert early.status_code == 500
        assert await IntegrationWebhookEvent.find_all().count() == 0
        assert await SiteOrder.find_all().count() == 0
        await _post(c, _paid(sid), eid="evt_p")
        again = await _post(c, refund, eid="evt_r")  # Razorpay's redelivery
        status = (await SiteOrder.find_one()).status
    assert again.json() == {"ok": True} and status == "refunded"


@pytest.mark.asyncio
async def test_test_mode_is_refused_in_production(beanie_test_db, rzp, monkeypatch, caplog):
    from pocketpaw_ee.integrations.providers.razorpay import RazorpayProvider

    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("POCKETPAW_INTEGRATIONS_RAZORPAY_MODE", "test")
    assert RazorpayProvider().availability() == "available"  # dev: fine
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    assert RazorpayProvider().availability() == "unconfigured"
    assert "provider disabled" in caplog.text
    async with _client() as c:
        refused = await c.post("/api/v1/integrations/razorpay/connect", json={})
    assert refused.status_code == 409
    monkeypatch.delenv("POCKETPAW_INTEGRATIONS_RAZORPAY_MODE")
    assert RazorpayProvider().availability() == "available"
