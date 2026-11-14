# tests/ee/sites/test_integrations_refunds.py: owner refunds on site orders.
#
# ``POST /sites/by-pocket/{pocket_id}/orders/{order_id}/refunds`` end to end
# against the Stripe and Razorpay fakes of their own test files (refund endpoints
# added here). Covers: a partial refund in ISO 4217 minor units recorded on the
# order, the bound (never more than what is left, computed server-side), the
# remainder as the default amount, the owner's idempotency key (a replay never
# refunds twice) and the provider idempotency key, amount validation, owner /
# admin only, workspace scoping, an order from another provider account refused,
# a provider failure releasing the reservation, webhook refunds counted once next
# to ours, Razorpay's payment id (stored, or looked up for an older order), and
# providers without refunds refused.
# ruff: noqa: F811 - pytest fixtures imported from sibling test modules
from __future__ import annotations

import json
from urllib.parse import parse_qsl

import httpx
import pytest

from tests.ee.sites.test_integrations import (  # noqa: F401 - _env is an autouse fixture
    OTHER_WS,
    OWNER,
    WS,
    _client,
    _env,
    _pocket,
    _site,
)
from tests.ee.sites.test_integrations_razorpay import (  # noqa: F401 - fixtures
    FakeRazorpay,
    rzp,
)
from tests.ee.sites.test_integrations_razorpay import _paid as _rzp_paid
from tests.ee.sites.test_integrations_razorpay import _post as _rzp_post
from tests.ee.sites.test_integrations_razorpay import _ready_site as _rzp_ready
from tests.ee.sites.test_integrations_razorpay import _refund as _rzp_refund_event
from tests.ee.sites.test_integrations_stripe import (  # noqa: F401 - fixtures
    _evt,
    _post,
    _ready_site,
    _session_event,
    catalog,
    stripe,
)


@pytest.fixture
def stripe_refunds(stripe, monkeypatch):
    """The Stripe fake plus ``POST /v1/refunds``; ``state["status"]`` forces an error."""
    from pocketpaw_ee.integrations.providers import stripe as mod

    state: dict = {"calls": [], "status": 200, "refund_status": "succeeded"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/refunds":
            body = dict(parse_qsl(request.content.decode()))
            state["calls"].append((body, dict(request.headers)))
            if state["status"] != 200:
                return httpx.Response(state["status"], json={"error": {"code": "charge_disputed"}})
            n = len(state["calls"])
            return httpx.Response(
                200,
                json={
                    "id": f"re_{n}",
                    "object": "refund",
                    "amount": int(body["amount"]),
                    "status": state["refund_status"],
                },
            )
        return stripe.handler(request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        mod, "_client", lambda: httpx.AsyncClient(base_url=mod.API_BASE, transport=transport)
    )
    return state


async def _paid_stripe_order(c, stripe, catalog) -> tuple[str, str, str]:
    """A paid 3750 USD order on pi_1 (acct_0001). Returns (pocket, site, order id)."""
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    pid, sid, _ = await _ready_site(c, stripe, catalog)
    resp = await _post(c, _session_event("evt_paid", sid, "acct_0001"))
    assert resp.json() == {"ok": True}
    [order] = await SiteOrder.find_all().to_list()
    return pid, sid, str(order.id)


def _url(pid: str, order_id: str) -> str:
    return f"/api/v1/sites/by-pocket/{pid}/orders/{order_id}/refunds"


async def _refund(c, pid: str, oid: str, key: str, amount: int | None = None):
    body: dict = {"idempotency_key": key}
    if amount is not None:
        body["amount_minor"] = amount
    return await c.post(_url(pid, oid), json=body)


async def _order(oid: str):
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    return await SiteOrder.get(PydanticObjectId(oid))


# ------------------------------------------------------------------- stripe


@pytest.mark.asyncio
async def test_a_partial_refund_is_recorded_and_bounds_the_rest(
    beanie_test_db, stripe, stripe_refunds, catalog
):
    async with _client() as c:
        pid, _, oid = await _paid_stripe_order(c, stripe, catalog)
        first = await _refund(c, pid, oid, "key-000000001", 1000)
        assert first.status_code == 200, first.text
        view = first.json()
        assert view["status"] == "paid"
        assert view["refunded_minor"] == 1000 and view["refundable_minor"] == 2750
        assert view["can_refund"] is True
        [entry] = view["refunds"]
        assert entry["amount_minor"] == 1000 and entry["status"] == "succeeded"

        [(body, headers)] = stripe_refunds["calls"]
        assert body["payment_intent"] == "pi_1" and body["amount"] == "1000"
        assert headers["stripe-account"] == "acct_0001"
        assert headers["idempotency-key"] == entry["id"]

        # More than what is left never reaches Stripe.
        over = await _refund(c, pid, oid, "key-000000002", 2751)
        assert over.status_code == 422, over.text
        assert over.json()["error"]["code"] == "integrations.refund_too_large"
        assert len(stripe_refunds["calls"]) == 1

        # No amount: the rest.
        rest = await _refund(c, pid, oid, "key-000000003")
        assert rest.status_code == 200, rest.text
        view = rest.json()
        assert view["status"] == "refunded" and view["refunded_minor"] == 3750
        assert view["refundable_minor"] == 0 and view["can_refund"] is False
        assert [r["amount_minor"] for r in view["refunds"]] == [1000, 2750]

        done = await _refund(c, pid, oid, "key-000000004", 1)
        assert done.status_code == 409
        assert done.json()["error"]["code"] == "integrations.not_refundable"
    assert len(stripe_refunds["calls"]) == 2
    order = await _order(oid)
    assert order.status == "refunded"


@pytest.mark.asyncio
async def test_the_same_key_never_refunds_twice(beanie_test_db, stripe, stripe_refunds, catalog):
    async with _client() as c:
        pid, _, oid = await _paid_stripe_order(c, stripe, catalog)
        a = await _refund(c, pid, oid, "same-key-0001", 500)
        b = await _refund(c, pid, oid, "same-key-0001", 500)
        # A different amount under the same key is a replay too: the first one stands.
        d = await _refund(c, pid, oid, "same-key-0001", 900)
    assert a.status_code == b.status_code == d.status_code == 200
    assert len(stripe_refunds["calls"]) == 1
    assert b.json()["refunded_minor"] == d.json()["refunded_minor"] == 500


@pytest.mark.asyncio
async def test_amounts_are_positive_integers_in_minor_units(
    beanie_test_db, stripe, stripe_refunds, catalog
):
    async with _client() as c:
        pid, _, oid = await _paid_stripe_order(c, stripe, catalog)
        for bad in (0, -5, 1.5, "100", True, None):
            resp = await c.post(
                _url(pid, oid), json={"idempotency_key": "key-000000009", "amount_minor": bad}
            )
            if bad is None:
                continue  # None means "the rest": checked by the bound test
            assert resp.status_code == 422, (bad, resp.text)
        for key in ("", "short", "bad key with spaces", "x" * 101):
            resp = await c.post(_url(pid, oid), json={"idempotency_key": key, "amount_minor": 1})
            assert resp.status_code == 422, (key, resp.text)
    # Only the None case refunded (the full rest); every bad one stopped first.
    assert len(stripe_refunds["calls"]) == 1


@pytest.mark.asyncio
async def test_only_the_owner_or_an_admin_refunds_and_only_in_their_workspace(
    beanie_test_db, stripe, stripe_refunds, catalog, monkeypatch
):
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async with _client() as c:
        pid, _, oid = await _paid_stripe_order(c, stripe, catalog)

    async def _can_edit(_pocket_id: str, _user_id: str) -> bool:
        return True

    monkeypatch.setattr(pockets_service, "has_edit_access", _can_edit)
    async with _client(WS, "user-editor") as editor:
        denied = await _refund(editor, pid, oid, "key-000000010", 100)
    async with _client(OTHER_WS, OWNER) as stranger:
        missing = await _refund(stranger, pid, oid, "key-000000011", 100)
    assert denied.status_code == 403
    assert missing.status_code == 404
    assert stripe_refunds["calls"] == []


@pytest.mark.asyncio
async def test_an_order_from_another_account_is_refused(
    beanie_test_db, stripe, stripe_refunds, catalog
):
    async with _client() as c:
        pid, _, oid = await _paid_stripe_order(c, stripe, catalog)
        order = await _order(oid)
        assert order.account_ref == "acct_0001"
        order.account_ref = "acct_someone_else"
        await order.save()
        resp = await _refund(c, pid, oid, "key-000000020", 100)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "integrations.refund_account_mismatch"
    assert stripe_refunds["calls"] == []


@pytest.mark.asyncio
async def test_a_provider_failure_releases_the_reservation(
    beanie_test_db, stripe, stripe_refunds, catalog
):
    async with _client() as c:
        pid, _, oid = await _paid_stripe_order(c, stripe, catalog)
        stripe_refunds["status"] = 400
        failed = await _refund(c, pid, oid, "key-000000030", 3750)
        assert failed.status_code == 502
        assert failed.json()["error"]["code"] == "provider_error"
        order = await _order(oid)
        assert [r.status for r in order.refunds] == ["failed"]
        stripe_refunds["status"] = 200
        again = await _refund(c, pid, oid, "key-000000031", 3750)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "refunded"


@pytest.mark.asyncio
async def test_a_pending_provider_refund_still_counts(
    beanie_test_db, stripe, stripe_refunds, catalog
):
    async with _client() as c:
        pid, _, oid = await _paid_stripe_order(c, stripe, catalog)
        stripe_refunds["refund_status"] = "pending"
        resp = await _refund(c, pid, oid, "key-000000040", 3000)
        assert resp.json()["refunds"][0]["status"] == "pending"
        assert resp.json()["refundable_minor"] == 750
        over = await _refund(c, pid, oid, "key-000000041", 751)
    assert over.status_code == 422


@pytest.mark.asyncio
async def test_webhook_refunds_count_once_next_to_ours(
    beanie_test_db, stripe, stripe_refunds, catalog
):
    async with _client() as c:
        pid, _, oid = await _paid_stripe_order(c, stripe, catalog)
        await _refund(c, pid, oid, "key-000000050", 1000)
        charge = {"id": "ch_1", "payment_intent": "pi_1", "metadata": {}, "amount": 3750}
        # Stripe reports the cumulative amount: ours, not on top of ours.
        ours = await _post(
            c, _evt("evt_r1", "charge.refunded", {**charge, "amount_refunded": 1000})
        )
        assert ours.json() == {"ok": True}
        assert (await _order(oid)).status == "paid"
        view = (await c.get(f"/api/v1/sites/by-pocket/{pid}/orders")).json()["items"][0]
        assert view["refunded_minor"] == 1000 and view["refundable_minor"] == 2750
        # A refund made in the Stripe dashboard on top of it.
        await _post(c, _evt("evt_r2", "charge.refunded", {**charge, "amount_refunded": 1500}))
        view = (await c.get(f"/api/v1/sites/by-pocket/{pid}/orders")).json()["items"][0]
        assert view["refunded_minor"] == 1500 and view["refundable_minor"] == 2250
        over = await _refund(c, pid, oid, "key-000000051", 2251)
        assert over.status_code == 422
        await _post(c, _evt("evt_r3", "charge.refunded", {**charge, "amount_refunded": 3750}))
    order = await _order(oid)
    assert order.status == "refunded"


@pytest.mark.asyncio
async def test_unpaid_orders_and_providers_without_refunds_are_refused(beanie_test_db):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    pid = await _pocket()
    await _site(pid)
    pending = SiteOrder(
        workspace=WS, pocket_id=pid, provider="stripe", external_id="pi_p", status="pending",
        amount_minor=100, currency="USD",
    )  # fmt: skip
    demo = SiteOrder(
        workspace=WS, pocket_id=pid, provider="demo", external_id="d_1", status="paid",
        amount_minor=100, currency="USD",
    )  # fmt: skip
    await pending.insert()
    await demo.insert()
    async with _client() as c:
        a = await _refund(c, pid, str(pending.id), "key-000000060", 50)
        b = await _refund(c, pid, str(demo.id), "key-000000061", 50)
        listed = (await c.get(f"/api/v1/sites/by-pocket/{pid}/orders")).json()["items"]
    assert a.status_code == 409 and a.json()["error"]["code"] == "integrations.not_refundable"
    assert b.status_code == 409 and b.json()["error"]["code"] == "integrations.not_refundable"
    assert all(o["can_refund"] is False for o in listed)


# ----------------------------------------------------------------- razorpay


@pytest.fixture
def rzp_refunds(rzp, monkeypatch):
    """The Razorpay fake plus its refund and order-payments endpoints."""
    from pocketpaw_ee.integrations.providers import oauth_http

    state: dict = {"calls": [], "lookups": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.razorpay.com" and url.path.endswith("/refund"):
            auth = request.headers.get("authorization", "").removeprefix("Bearer ")
            if auth not in rzp.live_access:
                return httpx.Response(401, json={"error": {"code": "BAD_REQUEST_ERROR"}})
            body = json.loads(request.content)
            state["calls"].append((url.path, body, dict(request.headers)))
            n = len(state["calls"])
            return httpx.Response(
                200, json={"id": f"rfnd_{n}", "amount": body["amount"], "status": "processed"}
            )
        if url.host == "api.razorpay.com" and url.path == "/v1/orders/order_A/payments":
            state["lookups"] += 1
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "pay_failed", "status": "failed"},
                        {"id": "pay_1", "status": "captured"},
                    ]
                },
            )
        return rzp.handle(request)

    monkeypatch.setattr(oauth_http, "_transport", httpx.MockTransport(handler))
    return state


@pytest.mark.asyncio
async def test_razorpay_partial_refund_on_the_stored_payment(
    beanie_test_db, rzp, rzp_refunds, catalog
):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    async with _client() as c:
        pid, sid, _ = await _rzp_ready(c, catalog)
        assert (await _rzp_post(c, _rzp_paid(sid), eid="evt_1")).json() == {"ok": True}
        [order] = await SiteOrder.find_all().to_list()
        assert order.payment_ref == "pay_1" and order.account_ref == "acc_Merchant0001"
        resp = await _refund(c, pid, str(order.id), "rzp-key-00001", 100000)
        assert resp.status_code == 200, resp.text
        view = resp.json()
        assert view["refunded_minor"] == 100000 and view["refundable_minor"] == 275000
        [(path, body, headers)] = rzp_refunds["calls"]
        assert path == "/v1/payments/pay_1/refund"
        assert body["amount"] == 100000 and body["speed"] == "normal"
        entry = view["refunds"][0]
        assert headers["x-refund-idempotency"] == entry["id"] and body["receipt"] == entry["id"]
        assert rzp_refunds["lookups"] == 0

        # Razorpay's webhook for it reports the payment's cumulative refund.
        event = _rzp_refund_event("order_A", refunded=100000, status="partial")
        assert (await _rzp_post(c, event, eid="evt_2")).json() == {"ok": True}
        view = (await c.get(f"/api/v1/sites/by-pocket/{pid}/orders")).json()["items"][0]
        assert view["refunded_minor"] == 100000 and view["status"] == "paid"


@pytest.mark.asyncio
async def test_razorpay_looks_up_the_payment_of_an_older_order(
    beanie_test_db, rzp, rzp_refunds, catalog
):
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    async with _client() as c:
        pid, sid, _ = await _rzp_ready(c, catalog)
        await _rzp_post(c, _rzp_paid(sid), eid="evt_1")
        [order] = await SiteOrder.find_all().to_list()
        order.payment_ref = ""  # recorded before payment ids were kept
        await order.save()
        resp = await _refund(c, pid, str(order.id), "rzp-key-00002")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "refunded"
    assert rzp_refunds["lookups"] == 1
    assert rzp_refunds["calls"][0][0] == "/v1/payments/pay_1/refund"
    assert rzp_refunds["calls"][0][1]["amount"] == 375000
