# tests/cloud/billing/test_payments_closed.py — while ``payments_open`` is off
# (early access) the server opens no checkout and no webhook mints value.
#
# * every creation path (top-up, plan subscribe, partner pay link) refuses with
#   403 ``payments.closed`` when closed, and calls the provider when open;
# * the HTTP routes carry that refusal as 403 with the code;
# * a verified ``payment.succeeded`` or ``subscription.active`` that arrives while
#   closed, or open but in Dodo test mode, grants nothing and changes no plan,
#   and the payment is recorded with ``credits_granted=0`` for review.
#
# The billing conftest opens payments for the rest of the suite; each test here
# sets its own posture with ``_posture``, which runs after it and wins.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.billing import plans
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.payment import Payment
from pocketpaw_ee.cloud.models.workspace import Workspace
from standardwebhooks import Webhook

pytestmark = pytest.mark.asyncio

SECRET = "whsec_" + base64.b64encode(b"billing-test-secret-key-32bytes!").decode()
PLAN_PRODUCTS = {"go": "prod_go_recurring", "pro": "prod_pro_recurring"}


def _posture(monkeypatch, *, open_: bool, env: str = "live_mode") -> None:
    import pocketpaw.config as config

    settings = config.get_settings().model_copy(
        update={"payments_open": open_, "dodo_environment": env}
    )
    monkeypatch.setattr(billing, "get_settings", lambda *a, **k: settings)


def _mock_provider() -> MagicMock:
    prov = MagicMock()
    checkout = SimpleNamespace(checkout_url="https://checkout.test/x", gateway_ref="pay_1")
    prov.create_one_time = AsyncMock(return_value=checkout)
    prov.create_subscription = AsyncMock(return_value=checkout)
    return prov


@pytest.fixture
def plan_products(monkeypatch):
    monkeypatch.setattr(plans, "_dodo_product_for", lambda key: PLAN_PRODUCTS.get(key))


async def _create_topup(prov):
    return await billing.create_topup(
        workspace_id="ws1", user_id="u1", amount_credits=1000, provider=prov
    )


async def _subscribe(prov):
    return await billing.subscribe(workspace_id="ws1", user_id="u1", plan_key="pro", provider=prov)


async def _partner_checkout(prov):
    return await billing.create_partner_client_checkout(
        workspace_id="ws1",
        user_id="u1",
        site_id="site1",
        amount_minor=22800,
        currency="USD",
        provider=prov,
    )


_CREATORS = [
    (_create_topup, "create_one_time"),
    (_subscribe, "create_subscription"),
    (_partner_checkout, "create_one_time"),
]


@pytest.mark.parametrize("create,provider_call", _CREATORS)
async def test_creation_refused_while_closed(
    mongo_db, monkeypatch, plan_products, create, provider_call
):
    _posture(monkeypatch, open_=False)
    prov = _mock_provider()
    with pytest.raises(Forbidden) as exc:
        await create(prov)
    assert exc.value.code == "payments.closed"
    assert exc.value.status_code == 403
    getattr(prov, provider_call).assert_not_awaited()


@pytest.mark.parametrize("create,provider_call", _CREATORS)
async def test_creation_allowed_while_open(
    mongo_db, monkeypatch, plan_products, create, provider_call
):
    _posture(monkeypatch, open_=True)
    prov = _mock_provider()
    await create(prov)
    getattr(prov, provider_call).assert_awaited_once()


def _billing_client(monkeypatch) -> TestClient:
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.billing.router import router as billing_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.guards import deps as guards_deps

    monkeypatch.setattr(guards_deps, "_has_action_override", AsyncMock(return_value=False))
    user = SimpleNamespace(
        id="u-owner",
        active_workspace="ws-billing",
        workspaces=[SimpleNamespace(workspace="ws-billing", role="owner")],
    )

    async def _user():
        return user

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = _user
    app.include_router(billing_router)
    return TestClient(app, raise_server_exceptions=False)


_ROUTES = [
    ("/billing/topup", {"amount_credits": 1000}),
    ("/billing/subscribe", {"plan_key": "pro"}),
]


@pytest.mark.parametrize("path,body", _ROUTES)
async def test_http_route_refused_while_closed(mongo_db, monkeypatch, path, body):
    _posture(monkeypatch, open_=False)
    prov = _mock_provider()
    monkeypatch.setattr(billing, "_default_provider", lambda: prov)
    resp = _billing_client(monkeypatch).post(path, json=body)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "payments.closed"
    prov.create_one_time.assert_not_awaited()
    prov.create_subscription.assert_not_awaited()


async def test_http_topup_allowed_while_open(mongo_db, monkeypatch):
    _posture(monkeypatch, open_=True)
    prov = _mock_provider()
    monkeypatch.setattr(billing, "_default_provider", lambda: prov)
    resp = _billing_client(monkeypatch).post("/billing/topup", json={"amount_credits": 1000})
    assert resp.status_code == 200, resp.text
    assert resp.json()["checkout_url"] == "https://checkout.test/x"


# --- webhooks ----------------------------------------------------------------


def _provider() -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id="prod_credits_sku",
        plan_products=PLAN_PRODUCTS,
    )


def _signed(payload: dict, msg_id: str) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload)
    ts = datetime.now(UTC)
    sig = Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body)
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": sig,
    }
    return body.encode(), headers


async def _workspace() -> str:
    ws = Workspace(name="Acme", slug="acme-closed", owner="u-owner", plan="free")
    await ws.insert()
    return str(ws.id)


def _topup_paid(ws: str) -> dict:
    return {
        "business_id": "biz_1",
        "type": "payment.succeeded",
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {
            "payment_id": "pay_test_card",
            "metadata": {"workspace_id": ws},
            "total_amount": 100000,
            "currency": "USD",
        },
    }


@pytest.mark.parametrize(
    "open_,env",
    [(False, "test_mode"), (False, "live_mode"), (True, "test_mode")],
    ids=["closed-test", "closed-live", "open-test"],
)
async def test_topup_webhook_grants_nothing_unless_open_and_live(mongo_db, monkeypatch, open_, env):
    _posture(monkeypatch, open_=open_, env=env)
    ws = await _workspace()
    payload, headers = _signed(_topup_paid(ws), "evt_closed_topup")

    result = await billing.handle_webhook(payload=payload, headers=headers, provider=_provider())

    assert result == {"ok": True, "granted": False}
    assert await credits.balance(ws) == 0
    rows = await Payment.find(Payment.workspace == ws).to_list()
    assert len(rows) == 1, "the refused payment must be recorded for review"
    assert rows[0].credits_granted == 0


async def test_topup_webhook_grants_when_open_and_live(mongo_db, monkeypatch):
    _posture(monkeypatch, open_=True, env="live_mode")
    ws = await _workspace()
    payload, headers = _signed(_topup_paid(ws), "evt_open_topup")

    result = await billing.handle_webhook(payload=payload, headers=headers, provider=_provider())

    assert result == {"ok": True, "granted": True}
    assert await credits.balance(ws) == 100000


async def test_subscription_active_while_closed_changes_nothing(mongo_db, monkeypatch):
    _posture(monkeypatch, open_=False, env="test_mode")
    ws = await _workspace()
    payload, headers = _signed(
        {
            "business_id": "biz_1",
            "type": "subscription.active",
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "subscription_id": "sub_test_card",
                "product_id": "prod_pro_recurring",
                "metadata": {"workspace_id": ws, "plan_key": "pro"},
            },
        },
        "evt_closed_sub",
    )

    result = await billing.handle_webhook(payload=payload, headers=headers, provider=_provider())

    assert result == {"ok": True, "granted": False}
    assert await credits.balance(ws) == 0
    assert (await Workspace.get(ws)).plan == "free"
