# tests/ee/sites/test_integrations_stripe.py: the ``stripe`` Sites integration.
#
# Stripe's REST API is a fake behind ``httpx.MockTransport`` (``_FakeStripe``):
# v2 accounts (JSON bodies, the v2 ``Stripe-Version``), v2 account links and v1
# checkout sessions. Covers the Account Link connect flow (v2 create body,
# server-side account binding, pending-account reuse, resume), meta from a v2
# account, a Stripe failure at connect/resume as a generic 502 (and the "platform
# has no Connect" warning), attention and site status from connection meta,
# ``create_checkout`` (server-side catalog pricing, currency, qty bounds,
# success/cancel URLs, platform fee off, ``Stripe-Account`` = the v2 account id),
# and webhooks (signature, orders paid / refunded / failed, dedupe, v1
# account.updated and thin v2 account events refreshing meta, cross-account
# refusal). No network.
from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, parse_qsl, urlsplit

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

SK = "sk_test_platform"
WHSEC = "whsec_stripe_test"
SHOP = "https://shop.example.com"


def _v2_account(acct: str, body: dict) -> dict:
    """A fresh v2 merchant account as Stripe returns it with our includes."""
    return {
        "id": acct,
        "object": "v2.core.account",
        "contact_email": "owner@shop.test",
        "dashboard": body.get("dashboard"),
        "metadata": dict(body.get("metadata") or {}),
        "configuration": {
            "merchant": {
                "capabilities": {"card_payments": {"status": "restricted", "status_details": []}}
            }
        },
        "requirements": {
            "entries": [
                {
                    "awaiting_action_from": "user",
                    "description": "external_account",
                    "minimum_deadline": {"status": "currently_due"},
                },
                {
                    "awaiting_action_from": "user",
                    "description": "business_profile.url",
                    "minimum_deadline": {"status": "eventually_due"},
                },
            ]
        },
    }


def _onboarded(account: dict) -> None:
    """Flip a fake v2 account to fully onboarded (card payments active)."""
    caps = account["configuration"]["merchant"]["capabilities"]
    caps["card_payments"]["status"] = "active"
    account["requirements"] = {"entries": []}


class _FakeStripe:
    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.bodies: dict[str, dict] = {}
        self.links: list[dict] = []
        self.sessions: list[dict] = []
        self.headers: list[dict] = []
        self.paths: list[str] = []
        self.fail_create: tuple[int, dict] | None = None
        self.fail_link: tuple[int, dict] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(dict(request.headers))
        self.paths.append(str(request.url.raw_path.decode()))
        path = request.url.path
        if path.startswith("/v2/"):
            assert request.headers["stripe-version"] == "2026-09-30.endive"
            body = json.loads(request.content) if request.content else {}
            if request.content:
                assert request.headers["content-type"] == "application/json"
        else:
            assert request.headers["stripe-version"] == "2025-08-27.basil"
            body = dict(parse_qsl(request.content.decode())) if request.content else {}
        if path == "/v2/core/accounts" and request.method == "POST":
            if self.fail_create is not None:
                return httpx.Response(self.fail_create[0], json=self.fail_create[1])
            merchant = (body.get("configuration") or {}).get("merchant")
            if merchant and not (body.get("identity") or {}).get("country"):
                # Stripe's real answer (live-tested): country comes before merchant.
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "code": "identity_country_required",
                            "message": "The field identity.country is required before "
                            "setting configuration.merchant.",
                        }
                    },
                )
            acct = f"acct_{len(self.accounts) + 1:04d}"
            self.bodies[acct] = body
            self.accounts[acct] = _v2_account(acct, body)
            return httpx.Response(200, json={"id": acct, "object": "v2.core.account"})
        if path.startswith("/v2/core/accounts/") and request.method == "GET":
            includes = {v for k, v in request.url.params.multi_items() if k.startswith("include[")}
            assert {"configuration.merchant", "requirements"} <= includes
            acct = self.accounts.get(path.rsplit("/", 1)[1])
            if acct is None:
                return httpx.Response(404, json={"error": {"type": "not_found"}})
            return httpx.Response(200, json=acct)
        if path == "/v2/core/account_links" and request.method == "POST":
            if self.fail_link is not None:
                return httpx.Response(self.fail_link[0], json=self.fail_link[1])
            self.links.append(body)
            n = len(self.links)
            return httpx.Response(200, json={"url": f"https://connect.stripe.com/setup/s/{n}"})
        if path == "/v1/checkout/sessions":
            self.sessions.append(body)
            return httpx.Response(200, json={"url": "https://checkout.stripe.com/c/pay/cs_1"})
        return httpx.Response(404, json={"error": {"type": "not_found"}})


def _onboarding(link: dict) -> dict:
    assert link["use_case"]["type"] == "account_onboarding"
    return link["use_case"]["account_onboarding"]


@pytest.fixture
def stripe(monkeypatch):
    from pocketpaw_ee.integrations.providers import stripe as mod

    monkeypatch.setenv(mod.SECRET_KEY_ENV, SK)
    monkeypatch.setenv(mod.WEBHOOK_SECRET_ENV, WHSEC)
    fake = _FakeStripe()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(
        mod, "_client", lambda: httpx.AsyncClient(base_url=mod.API_BASE, transport=transport)
    )
    return fake


@pytest.fixture
async def catalog(tmp_path, monkeypatch):
    """A Paw Bar store with one widget per pocket on demand; returns an adder."""
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


async def _start(c, pocket_id: str | None = None) -> tuple[str, dict]:
    """connect -> /start; returns (Stripe link url, the link's form)."""
    body = {"pocket_id": pocket_id} if pocket_id else {}
    started = await c.post("/api/v1/integrations/stripe/connect", json=body)
    assert started.status_code == 200, started.text
    split = urlsplit(started.json()["authorize_url"])
    resp = await c.get(split.path, params=parse_qs(split.query))
    assert resp.status_code == 302, resp.text
    return resp.headers["location"], {}


async def _return(c, link_form: dict, **override) -> dict:
    """Follow the link's return_url back to our callback (Stripe's redirect)."""
    query = dict(parse_qsl(urlsplit(_onboarding(link_form)["return_url"]).query))
    query.update(override)
    cb = await c.get("/api/v1/integrations/stripe/callback", params=query)
    assert cb.status_code == 200
    return _message(cb.text)


async def _connect(c, fake: _FakeStripe, pocket_id: str | None = None) -> dict:
    await _start(c, pocket_id)
    return await _return(c, fake.links[-1])


async def _conn(conn_id: str):
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    return await IntegrationConnection.get(PydanticObjectId(conn_id))


# ------------------------------------------------------------------ catalog


@pytest.mark.asyncio
async def test_unconfigured_without_the_platform_key(beanie_test_db, monkeypatch):
    from pocketpaw_ee.integrations.providers import stripe as mod

    monkeypatch.delenv(mod.SECRET_KEY_ENV, raising=False)
    async with _client() as c:
        views = {p["id"]: p for p in (await c.get("/api/v1/integrations/providers")).json()}
        refused = await c.post("/api/v1/integrations/stripe/connect", json={})
    view = views["stripe"]
    assert (view["kind"], view["category"], view["availability"]) == (
        "connect",
        "payments",
        "unconfigured",
    )
    assert [f["key"] for f in view["settings_fields"]] == [
        "currency",
        "success_path",
        "cancel_path",
    ]
    assert [a["name"] for a in view["actions"]] == ["create_checkout"]
    assert refused.status_code == 409


# ------------------------------------------------------------------ connect


@pytest.mark.asyncio
async def test_connect_creates_a_controller_account_and_binds_it(beanie_test_db, stripe):
    pid = await _pocket()
    async with _client() as c:
        location, _ = await _start(c, pid)
        assert location == "https://connect.stripe.com/setup/s/1"
        [acct] = stripe.accounts
        body = stripe.bodies[acct]
        assert body == {
            "dashboard": "full",
            "identity": {"country": "US"},
            "configuration": {"merchant": {"capabilities": {"card_payments": {"requested": True}}}},
            "defaults": {
                "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"}
            },
            "metadata": {"workspace_id": WS, "user_id": OWNER},
        }
        assert "POST /v1/accounts" not in " ".join(stripe.paths)
        link = stripe.links[0]
        onboarding = _onboarding(link)
        assert link["account"] == acct
        assert onboarding["return_url"].startswith(
            "https://connect.example.com/api/v1/integrations/stripe/callback?state="
        )
        assert f"code={acct}" in onboarding["return_url"]
        assert "code=" not in onboarding["refresh_url"]
        assert all(h["authorization"] == f"Bearer {SK}" for h in stripe.headers)

        msg = await _return(c, link)
        assert msg["status"] == "active", msg
        sites = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
        listed = (await c.get("/api/v1/integrations/connections")).json()

    conn = await _conn(msg["connection_id"])
    assert conn.account_ref == acct and conn.account_label == "owner@shop.test"
    # Only the currently-due entry counts; the eventually-due one doesn't.
    assert conn.meta == {
        "charges_enabled": False,
        "details_submitted": False,
        "requirements_due_count": 1,
    }
    [view] = listed
    assert view["attention"] == "Finish setting up your Stripe account"
    assert "acct_" not in json.dumps({k: v for k, v in view.items() if k != "id"})
    [site] = [s for s in sites if s["provider"] == "stripe"]
    assert site["status"] == "setup_required"


@pytest.mark.asyncio
async def test_a_returned_code_other_than_the_bound_account_is_refused(beanie_test_db, stripe):
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    # Another real account on our platform (someone else's), fully onboarded.
    stripe.accounts["acct_9999"] = {"id": "acct_9999", "charges_enabled": True}
    async with _client() as c:
        await _start(c)
        forged = await _return(c, stripe.links[-1], code="acct_9999")
    assert forged["status"] == "error" and forged["error"] == "provider_error"
    assert await IntegrationConnection.find_all().count() == 0


@pytest.mark.asyncio
async def test_restarting_connect_reuses_the_pending_account(beanie_test_db, stripe):
    async with _client() as c:
        await _start(c)
        await _start(c)
        assert len(stripe.accounts) == 1
        assert stripe.links[0]["account"] == stripe.links[1]["account"]
        # Fully onboarded but never connected: still theirs, still reused.
        _onboarded(stripe.accounts["acct_0001"])
        await _start(c)
        assert len(stripe.accounts) == 1
        msg = await _return(c, stripe.links[-1])
        assert msg["status"] == "active"
        # Once a connection holds it, the next connect makes a new account.
        await _start(c)
    assert len(stripe.accounts) == 2


@pytest.mark.asyncio
async def test_onboarding_past_the_default_ttl_still_completes(beanie_test_db, stripe):
    from pocketpaw_ee.cloud._core import redis_client
    from pocketpaw_ee.integrations import oauth

    redis = redis_client.get_redis()
    async with _client() as c:
        started = await c.post("/api/v1/integrations/stripe/connect", json={})
        split = urlsplit(started.json()["authorize_url"])
        start = await c.get(split.path, params=parse_qs(split.query))
        state = dict(parse_qsl(urlsplit(_onboarding(stripe.links[-1])["return_url"]).query))[
            "state"
        ]
        assert 7000 < await redis.ttl(f"integrations_state:{state}") <= 7200
        assert "Max-Age=7200" in start.headers["set-cookie"]
        bind_keys = await redis.keys("integrations_stripe_bind:*")
        assert [7000 < await redis.ttl(k) <= 7200 for k in bind_keys] == [True]
        assert oauth.cookie_max_age("demo") == oauth.COOKIE_MAX_AGE == 600

        # Even past 2 h: the state is gone, the callback fails, and the next
        # connect picks the same (now verified) account back up.
        await redis.delete(f"integrations_state:{state}")
        _onboarded(stripe.accounts["acct_0001"])
        lost = await _return(c, stripe.links[-1])
        assert lost["error"] == "invalid_state"
        await _start(c)
        msg = await _return(c, stripe.links[-1])
    assert msg["status"] == "active" and len(stripe.accounts) == 1
    conn = await _conn(msg["connection_id"])
    assert conn.account_ref == "acct_0001" and conn.meta["charges_enabled"] is True


@pytest.mark.asyncio
async def test_account_metadata_must_match_the_flow(beanie_test_db, stripe):
    from pocketpaw_ee.cloud._core import redis_client
    from pocketpaw_ee.cloud.models.integration import IntegrationConnection

    async with _client() as c:
        await _start(c)
        # The account now claims another workspace: complete() refuses it ...
        stripe.accounts["acct_0001"]["metadata"]["workspace_id"] = "ws_other"
        refused = await _return(c, stripe.links[-1])
        assert refused["error"] == "provider_error"
        assert await IntegrationConnection.find_all().count() == 0
        # ... and a pending pointer to it is not reused.
        keys = await redis_client.get_redis().keys("integrations_stripe_pending:*")
        assert len(keys) == 1
        await _start(c)
    assert len(stripe.accounts) == 2


@pytest.mark.asyncio
async def test_resume_issues_a_fresh_link_for_the_same_account(beanie_test_db, stripe):
    async with _client() as c:
        msg = await _connect(c, stripe)
        conn_id = msg["connection_id"]
        resumed = await c.post(f"/api/v1/integrations/connections/{conn_id}/resume")
        assert resumed.status_code == 200, resumed.text
        split = urlsplit(resumed.json()["authorize_url"])
        start = await c.get(split.path, params=parse_qs(split.query))
        assert start.headers["location"] == "https://connect.stripe.com/setup/s/2"
        assert stripe.links[1]["account"] == "acct_0001"
        assert len(stripe.accounts) == 1

        _onboarded(stripe.accounts["acct_0001"])
        done = await _return(c, stripe.links[1])
        assert done == {**done, "status": "active", "connection_id": conn_id}
        listed = (await c.get("/api/v1/integrations/connections")).json()
        nothing = await c.post(f"/api/v1/integrations/connections/{conn_id}/resume")
    assert listed[0]["attention"] is None
    assert nothing.status_code == 409


def test_v2_meta_maps_capability_and_user_requirements():
    from pocketpaw_ee.integrations.providers.stripe import _label, _v2_meta

    def entry(who: str, status: str) -> dict:
        return {"awaiting_action_from": who, "minimum_deadline": {"status": status}}

    account = {
        "id": "acct_1",
        "configuration": {"merchant": {"capabilities": {"card_payments": {"status": "active"}}}},
        "requirements": {
            "entries": [
                entry("user", "past_due"),
                entry("user", "currently_due"),
                entry("user", "eventually_due"),
                entry("stripe", "currently_due"),  # Stripe is verifying: nothing to do
            ]
        },
    }
    assert _v2_meta(account) == {
        "charges_enabled": True,
        "details_submitted": False,
        "requirements_due_count": 2,
    }
    account["requirements"]["entries"] = [entry("stripe", "currently_due")]
    pending = {"configuration": {"merchant": {"capabilities": {"card_payments": {}}}}}
    assert _v2_meta(account)["details_submitted"] is True
    assert _v2_meta({"id": "acct_1"}) == {
        "charges_enabled": False,
        "details_submitted": False,
        "requirements_due_count": 0,
    }
    assert _v2_meta({**pending, "requirements": {"entries": []}})["charges_enabled"] is False
    assert _label({"id": "acct_1", "display_name": " Mug Co ", "contact_email": "a@b.c"}) == (
        "Mug Co"
    )
    business = {"identity": {"business_details": {"registered_name": "Mug Ltd"}}}
    assert _label({"id": "acct_1", **business}) == "Mug Ltd"
    assert _label({"id": "acct_1"}) == "acct_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        {"type": "invalid_request_error", "code": "platform_registration_required"},
        {
            "type": "invalid_request_error",
            "message": "You can only create new accounts if you've signed up for Connect",
        },
    ],
)
async def test_connect_on_a_platform_without_connect_is_a_502_and_a_warning(
    beanie_test_db, stripe, caplog, error
):
    import logging

    stripe.fail_create = (400, {"error": error})
    with caplog.at_level(logging.WARNING):
        async with _client() as c:
            resp = await c.post("/api/v1/integrations/stripe/connect", json={})
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "provider_error"
    assert "Couldn't start connecting to Stripe." in resp.text
    assert "Connect" not in resp.text
    assert "platform_registration" not in resp.text and "signed up" not in resp.text
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "not signed up for Connect" in logs
    assert "ProviderError (status 400)" in logs
    assert SK not in logs and "only create new accounts" not in logs


@pytest.mark.asyncio
async def test_a_stripe_failure_at_connect_or_resume_is_a_generic_502(beanie_test_db, stripe):
    async with _client() as c:
        stripe.fail_create = (500, {"error": {"type": "api_error", "message": "secret detail"}})
        connect = await c.post("/api/v1/integrations/stripe/connect", json={})
        stripe.fail_create = None
        msg = await _connect(c, stripe)
        stripe.fail_link = (400, {"error": {"type": "invalid_request_error", "code": "nope"}})
        resume = await c.post(f"/api/v1/integrations/connections/{msg['connection_id']}/resume")
    for resp in (connect, resume):
        assert resp.status_code == 502, resp.text
        assert resp.json()["error"]["code"] == "provider_error"
        assert "secret detail" not in resp.text and "nope" not in resp.text
        assert "Try again, or contact support if it keeps happening." in resp.text


@pytest.mark.asyncio
async def test_account_country_comes_from_the_workspace_else_the_platform_default(
    beanie_test_db, stripe, monkeypatch
):
    from pocketpaw_ee.cloud.models.workspace import PartnerProfile, Workspace
    from pocketpaw_ee.integrations.providers import stripe as mod

    monkeypatch.setenv(mod.DEFAULT_COUNTRY_ENV, "gb")
    assert await mod._account_country(WS) == "GB"  # not a workspace id: the default
    ws = Workspace(name="Mugs", slug="mugs-country", owner=OWNER)
    ws.partner = PartnerProfile(status="active", footer_name="Mugs", country="de")
    await ws.insert()
    ws_id = str(ws.id)
    assert await mod._account_country(ws_id) == "DE"
    ws.partner = None
    await ws.save()
    assert await mod._account_country(ws_id) == "GB"
    for bad in ("USA", "1A", "u"):
        monkeypatch.setenv(mod.DEFAULT_COUNTRY_ENV, bad)
        with pytest.raises(mod.ProviderError):
            await mod._account_country(ws_id)
    monkeypatch.delenv(mod.DEFAULT_COUNTRY_ENV)
    assert await mod._account_country(ws_id) == "US"
    assert [mod._iso2(v) for v in ("in", " fr ", "ü1", None)] == ["IN", "FR", "", ""]


# ------------------------------------------------------------------ checkout


async def _ready_site(c, stripe, catalog, *, settings: dict | None = None):
    pid = await _pocket()
    site = await _site(pid)
    site.url = SHOP + "/"
    await site.save()
    await catalog(
        pid,
        {"id": "mug", "name": "Big Mug", "price_cents": 1250, "currency": "USD"},
        {"id": "free", "name": "Sticker", "price_cents": 0, "currency": "USD"},
        {"id": "gone", "name": "Old Mug", "price_cents": 900, "currency": "USD", "in_stock": False},
        {"id": "yen", "name": "Tea", "price_cents": 1500, "currency": "JPY"},
    )
    msg = await _connect(c, stripe, pid)
    body = {"enabled_actions": ["create_checkout"], "settings": settings or {}}
    put = await c.put(f"/api/v1/sites/by-pocket/{pid}/integrations/stripe", json=body)
    assert put.status_code == 200, put.text
    return pid, str(site.id), msg["connection_id"]


@pytest.mark.asyncio
async def test_checkout_prices_from_the_catalog_never_the_client(beanie_test_db, stripe, catalog):
    async with _client() as c:
        _, sid, _ = await _ready_site(
            c, stripe, catalog, settings={"currency": "USD", "success_path": "/thanks"}
        )
        ok = await _gw(c, sid, "stripe", "create_checkout", args={"product_ref": "mug", "qty": 3})
        sneaky = await _gw(
            c,
            sid,
            "stripe",
            "create_checkout",
            args={"product_ref": "mug", "qty": 1, "unit_amount": 1},
        )
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"] == {"url": "https://checkout.stripe.com/c/pay/cs_1"}
    assert _code(sneaky) == "invalid_args"
    [session] = stripe.sessions
    assert session["line_items[0][price_data][unit_amount]"] == "1250"
    assert session["line_items[0][price_data][currency]"] == "usd"
    assert session["line_items[0][quantity]"] == "3"
    assert session["mode"] == "payment"
    assert session["success_url"] == f"{SHOP}/thanks"
    assert session["cancel_url"] == f"{SHOP}/"
    assert session["metadata[site_id]"] == sid
    assert session["payment_intent_data[metadata][product_ref]"] == "mug"
    assert not any("application_fee_amount" in k for k in session)
    assert stripe.headers[-1]["stripe-account"] == "acct_0001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"product_ref": "nope", "qty": 1},
        {"product_ref": "free", "qty": 1},
        {"product_ref": "gone", "qty": 1},
        {"product_ref": "yen", "qty": 1},  # binding currency is USD
        {"product_ref": "mug", "qty": 0},
        {"product_ref": "mug", "qty": 100},
        {"product_ref": "x" * 121, "qty": 1},
    ],
)
async def test_checkout_refuses_unusable_products_and_bad_qty(
    beanie_test_db, stripe, catalog, args
):
    async with _client() as c:
        _, sid, _ = await _ready_site(c, stripe, catalog, settings={"currency": "USD"})
        resp = await _gw(c, sid, "stripe", "create_checkout", args=args)
    assert resp.status_code == 422
    assert _code(resp) == "invalid_args"
    assert stripe.sessions == []


@pytest.mark.asyncio
async def test_checkout_uses_the_product_currency_without_a_setting_and_safe_paths(
    beanie_test_db, stripe, catalog, monkeypatch
):
    from pocketpaw_ee.cloud.models.site import SiteDomain
    from pocketpaw_ee.integrations.providers import stripe as mod

    monkeypatch.setattr(mod, "PLATFORM_FEE_BPS", 500)
    async with _client() as c:
        pid, sid, _ = await _ready_site(c, stripe, catalog, settings={"cancel_path": "/shop"})
        from beanie import PydanticObjectId
        from pocketpaw_ee.cloud.models.site import Site

        site = await Site.get(PydanticObjectId(sid))
        site.domains = [SiteDomain(hostname="buy.example.org", status="live")]
        await site.save()
        # A stored path that isn't one (set behind the PUT's back) reads as "/".
        from pocketpaw_ee.cloud.models.integration import SiteIntegration

        row = await SiteIntegration.find_one(SiteIntegration.pocket_id == pid)
        row.settings = {**row.settings, "success_path": "//evil.example/x"}
        await row.save()
        resp = await _gw(c, sid, "stripe", "create_checkout", args={"product_ref": "yen"})
    assert resp.status_code == 200, resp.text
    [session] = stripe.sessions
    assert session["line_items[0][price_data][currency]"] == "jpy"
    assert session["line_items[0][price_data][unit_amount]"] == "1500"
    assert session["success_url"] == "https://buy.example.org/"
    assert session["cancel_url"] == "https://buy.example.org/shop"
    assert session["payment_intent_data[application_fee_amount]"] == "75"


def test_safe_path_rules():
    from pocketpaw_ee.integrations.providers.stripe import _safe_path

    assert _safe_path("/thanks?x=1") == "/thanks?x=1"
    for bad in ("https://evil.test", "//evil.test", "thanks", "/a b", "/\\evil", None, "/" * 201):
        assert _safe_path(bad) == "/"


@pytest.mark.asyncio
async def test_settings_put_validates_currency_and_path_length(beanie_test_db, stripe):
    pid = await _pocket()
    url = f"/api/v1/sites/by-pocket/{pid}/integrations/stripe"
    async with _client() as c:
        bad_currency = await c.put(url, json={"settings": {"currency": "XYZ"}})
        long_path = await c.put(url, json={"settings": {"success_path": "/" + "a" * 200}})
        good = await c.put(url, json={"settings": {"currency": "EUR", "success_path": "/ok"}})
    assert bad_currency.status_code == 422 and long_path.status_code == 422
    assert good.status_code == 200 and good.json()["settings"]["currency"] == "EUR"


# ------------------------------------------------------------------ webhooks


def _signed(event: dict, *, secret: str = WHSEC, at: int | None = None) -> tuple[bytes, dict]:
    from pocketpaw_ee.integrations.providers.stripe import sign

    body = json.dumps(event).encode()
    return body, {"stripe-signature": sign(body, secret, at), "content-type": "application/json"}


def _evt(eid: str, kind: str, obj: dict, acct: str = "acct_0001") -> dict:
    return {"id": eid, "type": kind, "account": acct, "data": {"object": obj}}


def _session_event(
    eid: str,
    sid: str,
    acct: str,
    *,
    kind: str = "checkout.session.completed",
    pi: str = "pi_1",
    status: str = "paid",
) -> dict:
    return {
        "id": eid,
        "type": kind,
        "account": acct,
        "data": {
            "object": {
                "id": "cs_1",
                "payment_intent": pi,
                "payment_status": status,
                "amount_total": 3750,
                "currency": "usd",
                "customer_details": {"email": "buyer@x.io"},
                "metadata": {
                    "site_id": sid,
                    "product_ref": "mug",
                    "product_name": "Big Mug",
                    "qty": "3",
                },
            }
        },
    }


async def _post(c, event: dict, **kw):
    body, headers = _signed(event, **kw)
    return await c.post("/api/v1/integrations/webhooks/stripe", content=body, headers=headers)


@pytest.mark.asyncio
async def test_webhook_signature_valid_invalid_and_stale(beanie_test_db, stripe):
    from pocketpaw_ee.integrations.providers.stripe import verify_signature

    body = b'{"id":"evt_1"}'
    now = int(time.time())
    from pocketpaw_ee.integrations.providers.stripe import sign

    assert verify_signature(body, sign(body, WHSEC, now), WHSEC)
    assert not verify_signature(body, sign(body, "whsec_other", now), WHSEC)
    assert not verify_signature(body + b" ", sign(body, WHSEC, now), WHSEC)
    assert not verify_signature(body, sign(body, WHSEC, now - 301), WHSEC)
    assert not verify_signature(body, "v1=abc", WHSEC)
    assert not verify_signature(body, sign(body, WHSEC, now), "")
    async with _client() as c:
        forged = await _post(c, {"id": "evt_x", "type": "x"}, secret="whsec_other")
        stale = await _post(c, {"id": "evt_y", "type": "x"}, at=now - 600)
        ignored = await _post(c, {"id": "evt_z", "type": "customer.created"})
    assert forged.status_code == 401 and stale.status_code == 401
    assert ignored.status_code == 200 and ignored.json().get("ignored") is True


@pytest.mark.asyncio
async def test_orders_paid_refunded_failed_and_deduped(beanie_test_db, stripe, catalog):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    async with _client() as c:
        pid, sid, conn_id = await _ready_site(c, stripe, catalog)
        # A second site on the same Stripe account: account-only routing is ambiguous.
        pid2 = await _pocket()
        await _site(pid2)
        url2 = f"/api/v1/sites/by-pocket/{pid2}/integrations/stripe"
        assert (await c.put(url2, json={"connection_id": conn_id})).status_code == 200
        paid = await _post(c, _session_event("evt_1", sid, "acct_0001"))
        dup = await _post(c, _session_event("evt_1", sid, "acct_0001"))
        assert paid.json() == {"ok": True} and dup.json()["duplicate"] is True
        [order] = await SiteOrder.find_all().to_list()
        assert (order.external_id, order.status, order.amount_minor, order.currency) == (
            "pi_1",
            "paid",
            3750,
            "USD",
        )
        assert order.customer_email == "buyer@x.io" and order.pocket_id == pid
        assert [(i.ref, i.name, i.qty) for i in order.items] == [("mug", "Big Mug", 3)]

        # A late failure never undoes a paid order; a refund does settle it.
        failed = {"id": "pi_1", "metadata": {"site_id": sid}}
        await _post(c, _evt("evt_2", "payment_intent.payment_failed", failed))
        assert (await SiteOrder.find_one()).status == "paid"
        # A partial refund leaves the order paid.
        charge = {"id": "ch_1", "payment_intent": "pi_1", "metadata": {}, "amount": 3750}
        part = {**charge, "amount_refunded": 50}
        partial = await _post(c, _evt("evt_3a", "charge.refunded", part))
        assert partial.json()["ignored"] is True
        assert (await SiteOrder.find_one()).status == "paid"
        # A full refund with no site metadata routes by the order's PaymentIntent.
        full = {**charge, "amount_refunded": 3750, "refunded": True}
        refund = await _post(c, _evt("evt_3", "charge.refunded", full))
        assert refund.json() == {"ok": True}
        assert (await SiteOrder.find_one()).status == "refunded"
        # Refunded is terminal: a late "paid" redelivery doesn't undo it.
        await _post(c, _session_event("evt_3b", sid, "acct_0001"))
        assert (await SiteOrder.find_one()).status == "refunded"
        # The same refund from another account finds the order but is refused.
        other = await _post(c, _evt("evt_3c", "charge.refunded", full, acct="acct_x"))
        assert other.json() == {"ok": True, "routed": False}

        # Failure for an order that never existed makes no row; an async failure
        # of a pending order marks it failed.
        other = {"id": "pi_2", "metadata": {"site_id": sid}}
        await _post(c, _evt("evt_4", "payment_intent.payment_failed", other))
        assert await SiteOrder.find_all().count() == 1
        await _post(c, _session_event("evt_5", sid, "acct_0001", pi="pi_3", status="unpaid"))
        async_failed = "checkout.session.async_payment_failed"
        await _post(c, _session_event("evt_6", sid, "acct_0001", pi="pi_3", kind=async_failed))
        statuses = {o.external_id: o.status for o in await SiteOrder.find_all().to_list()}
    assert statuses == {"pi_1": "refunded", "pi_3": "failed"}


@pytest.mark.asyncio
async def test_an_event_from_another_account_is_refused(beanie_test_db, stripe, catalog):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    async with _client() as c:
        _, sid, _ = await _ready_site(c, stripe, catalog)
        resp = await _post(c, _session_event("evt_9", sid, "acct_other"))
    assert resp.json() == {"ok": True, "routed": False}
    assert await SiteOrder.find_all().count() == 0


@pytest.mark.asyncio
async def test_account_updated_refreshes_meta_and_tells_bound_sites(
    beanie_test_db, stripe, catalog, monkeypatch
):
    from pocketpaw_ee.integrations import bindings

    seen: list[tuple[str, str, str]] = []

    async def _emit(pocket, provider_id, status, user_id):
        seen.append((str(pocket.id), provider_id, status))

    async with _client() as c:
        pid, _, conn_id = await _ready_site(c, stripe, catalog)
        monkeypatch.setattr(bindings, "emit_changed", _emit)
        account = {
            "id": "acct_0001",
            "charges_enabled": True,
            "details_submitted": True,
            "requirements": {"currently_due": []},
        }
        event = _evt("evt_a", "account.updated", account)
        first = await _post(c, event)
        again = await _post(c, event)
        stranger = await _post(c, {**event, "id": "evt_b", "account": "acct_nobody"})
        sites = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
        listed = (await c.get("/api/v1/integrations/connections")).json()
    assert first.json() == {"ok": True} and again.json()["duplicate"] is True
    assert stranger.json() == {"ok": True, "routed": False}
    conn = await _conn(conn_id)
    assert conn.meta["charges_enabled"] is True and conn.meta["requirements_due_count"] == 0
    assert seen == [(pid, "stripe", "connected")]
    assert [s["status"] for s in sites if s["provider"] == "stripe"] == ["connected"]
    assert listed[0]["attention"] is None


@pytest.mark.asyncio
async def test_an_older_account_updated_does_not_undo_a_newer_one(beanie_test_db, stripe, catalog):
    async with _client() as c:
        _, _, conn_id = await _ready_site(c, stripe, catalog)
        ready = {"id": "acct_0001", "charges_enabled": True, "details_submitted": True}
        blocked = {"id": "acct_0001", "charges_enabled": False, "details_submitted": True}
        new = await _post(c, {**_evt("evt_new", "account.updated", ready), "created": 2000})
        old = await _post(c, {**_evt("evt_old", "account.updated", blocked), "created": 1000})
    assert new.json() == {"ok": True} and old.json() == {"ok": True, "stale": True}
    conn = await _conn(conn_id)
    assert conn.meta["charges_enabled"] is True and conn.meta["as_of"] == 2000


@pytest.mark.asyncio
async def test_catalog_lookup_is_strictly_this_workspace_across_its_widgets(
    beanie_test_db, tmp_path, monkeypatch
):
    from pocketpaw_ee.integrations.providers.stripe import catalog_product

    from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget
    from pocketpaw.paw_bar.store import PawBarStore

    store = PawBarStore(tmp_path / "pb2.db")
    monkeypatch.setattr("pocketpaw.stores.get_paw_bar_store", lambda: store)

    async def _widget(ws: str, item: dict) -> None:
        spec = PawBarSpec(widget_id="w", pocket_id="p1")
        w = await store.create_widget(
            PawBarWidget(pocket_id="p1", owner=OWNER, workspace_id=ws, spec=spec)
        )
        await store.upsert_catalog_items(w.id, [item], workspace_id=ws)

    await _widget("", {"id": "legacy", "name": "Legacy", "price_cents": 100})
    await _widget(WS, {"id": "first", "name": "First", "price_cents": 200})
    await _widget(WS, {"id": "second", "name": "Second", "price_cents": 300})
    assert await catalog_product(WS, "p1", "legacy") is None
    assert (await catalog_product(WS, "p1", "first")).price_cents == 200
    assert (await catalog_product(WS, "p1", "second")).price_cents == 300
    assert await catalog_product("ws_other", "p1", "first") is None


@pytest.mark.asyncio
async def test_a_bad_catalog_currency_is_invalid_args(beanie_test_db, stripe, catalog, monkeypatch):
    from types import SimpleNamespace

    from pocketpaw_ee.integrations.providers import stripe as mod

    async with _client() as c:
        _, sid, _ = await _ready_site(c, stripe, catalog)
        bad = SimpleNamespace(id="m", name="M", price_cents=100, currency="U1", in_stock=True)

        async def _bad(*_a):
            return bad

        monkeypatch.setattr(mod, "catalog_product", _bad)
        resp = await _gw(c, sid, "stripe", "create_checkout", args={"product_ref": "m"})
    assert resp.status_code == 422 and _code(resp) == "invalid_args"
    assert stripe.sessions == []


@pytest.mark.asyncio
async def test_an_early_refund_is_retried_and_applies_after_paid(beanie_test_db, stripe, catalog):
    from pocketpaw_ee.cloud.models.integration import IntegrationWebhookEvent, SiteOrder

    full = {"id": "ch_1", "payment_intent": "pi_1", "metadata": {}, "amount": 3750}
    refund = _evt("evt_r", "charge.refunded", {**full, "amount_refunded": 3750})
    async with _client() as c:
        _, sid, _ = await _ready_site(c, stripe, catalog)
        early = await _post(c, refund)
        assert early.status_code == 500
        assert await IntegrationWebhookEvent.find_all().count() == 0
        assert await SiteOrder.find_all().count() == 0
        await _post(c, _session_event("evt_p", sid, "acct_0001"))
        again = await _post(c, refund)  # Stripe's redelivery
        status = (await SiteOrder.find_one()).status
    assert again.json() == {"ok": True} and status == "refunded"


def _thin(eid: str, kind: str, acct: str = "acct_0001") -> dict:
    return {
        "id": eid,
        "object": "v2.core.event",
        "type": kind,
        "livemode": False,
        "created": "2026-10-01T10:00:00.000Z",
        "context": None,
        "related_object": {
            "id": acct,
            "type": "v2.core.account",
            "url": f"/v2/core/accounts/{acct}",
        },
    }


@pytest.mark.asyncio
async def test_a_thin_v2_account_event_refetches_the_account(
    beanie_test_db, stripe, catalog, monkeypatch
):
    from pocketpaw_ee.integrations.providers import stripe as mod

    thin_secret = "whsec_thin_destination"
    monkeypatch.setenv(mod.ACCOUNTS_WEBHOOK_SECRET_ENV, thin_secret)
    async with _client() as c:
        pid, _, conn_id = await _ready_site(c, stripe, catalog)
        _onboarded(stripe.accounts["acct_0001"])
        kind = "v2.core.account[requirements].updated"
        first = await _post(c, _thin("evt_t1", kind), secret=thin_secret)
        again = await _post(c, _thin("evt_t1", kind), secret=thin_secret)
        # A v1 snapshot generated before that fetch can't undo it.
        blocked = {"id": "acct_0001", "charges_enabled": False, "details_submitted": True}
        old = {**_evt("evt_v1", "account.updated", blocked), "created": int(time.time()) - 60}
        stale = await _post(c, old)
        unknown = await _post(c, _thin("evt_t2", kind, acct="acct_gone"), secret=thin_secret)
        other = await _post(
            c, _thin("evt_t3", "v2.core.account_person.updated"), secret=thin_secret
        )
        forged = await _post(c, _thin("evt_t4", kind), secret="whsec_wrong")
        sites = (await c.get(f"/api/v1/sites/by-pocket/{pid}/integrations")).json()
    assert first.json() == {"ok": True}, first.text
    assert again.json()["duplicate"] is True
    assert stale.json() == {"ok": True, "stale": True}
    assert unknown.json()["ignored"] is True and other.json()["ignored"] is True
    assert forged.status_code == 401
    conn = await _conn(conn_id)
    assert conn.meta["charges_enabled"] is True and conn.meta["requirements_due_count"] == 0
    assert conn.meta["details_submitted"] is True
    assert abs(conn.meta["as_of"] - time.time()) < 60
    assert [s["status"] for s in sites if s["provider"] == "stripe"] == ["connected"]


@pytest.mark.asyncio
async def test_a_thin_event_whose_fetch_fails_is_retried_unrecorded(
    beanie_test_db, stripe, catalog
):
    from pocketpaw_ee.cloud.models.integration import IntegrationWebhookEvent

    async with _client() as c:
        await _ready_site(c, stripe, catalog)
        stripe.accounts["acct_0001"] = "not an object"  # an answer we can't use
        resp = await _post(c, _thin("evt_t5", "v2.core.account.updated"))
    assert resp.status_code == 500
    assert (
        await IntegrationWebhookEvent.find(IntegrationWebhookEvent.event_id == "evt_t5").count()
        == 0
    )
