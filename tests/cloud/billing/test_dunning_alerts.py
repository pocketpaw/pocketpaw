# tests/cloud/billing/test_dunning_alerts.py — a failed payment must reach a
# human. Dunning (test_dunning.py) gets the state machine right, but on its own
# it is silent: on 2026-10-10 a workspace sat on hold for the whole 7-day grace
# window and dropped to Free with nobody told. These tests pin the alerts:
#
#   on_hold (first delivery) -> owners/admins get a "payment failed" notification
#   on_hold (redelivery)     -> no second notification
#   grace sweep suspends     -> owners/admins get a "moved to Free" notification
#   expired / failed         -> the same "moved to Free" notification
#   alert delivery breaks    -> the billing state change still lands
#
# ``list_admin_ids`` is stubbed: it is a plain membership query covered by the
# workspace tests, and seeding full User docs here would only add noise.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.models.subscription import Subscription
from pocketpaw_ee.cloud.notifications import service as notifications_service
from pocketpaw_ee.cloud.workspace import service as workspace_service
from standardwebhooks import Webhook

SECRET = "whsec_" + base64.b64encode(b"billing-test-secret-key-32bytes!").decode()
SUB_ID = "sub_alert_1"
ADMINS = ["u-owner", "u-admin"]
FAILED_KIND = "billing.payment_failed"
DOWNGRADED_KIND = "billing.plan_downgraded"


def _provider() -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id="prod_credits_sku",
        plan_products={"pro": "prod_pro_recurring"},
    )


async def _deliver(event_type: str, workspace_id: str, *, msg_id: str) -> dict:
    body = json.dumps(
        {
            "business_id": "biz_1",
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "subscription_id": SUB_ID,
                "product_id": "prod_pro_recurring",
                "metadata": {"workspace_id": workspace_id, "plan_key": "pro"},
            },
        }
    )
    ts = datetime.now(UTC)
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body),
    }
    return await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )


async def _make_workspace() -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(name="Acme", slug="acme-alerts", owner="u-owner", plan="pro")
    await ws.insert()
    return str(ws.id)


async def _kinds(user_id: str) -> list[str]:
    return [n.kind for n in await notifications_service.list_for_user(user_id)]


@pytest.fixture
def admins(monkeypatch: pytest.MonkeyPatch):
    async def _list_admin_ids(workspace_id: str) -> list[str]:
        return list(ADMINS)

    monkeypatch.setattr(workspace_service, "list_admin_ids", _list_admin_ids)


async def test_on_hold_tells_every_owner_and_admin(mongo_db, admins):
    ws = await _make_workspace()
    await _deliver("subscription.on_hold", ws, msg_id="m1")

    for user in ADMINS:
        notes = [
            n for n in await notifications_service.list_for_user(user) if n.kind == FAILED_KIND
        ]
        assert len(notes) == 1, user
        assert notes[0].workspace_id == ws
        assert "/settings/billing" in notes[0].body or notes[0].source is not None


async def test_a_redelivered_on_hold_does_not_notify_twice(mongo_db, admins):
    ws = await _make_workspace()
    await _deliver("subscription.on_hold", ws, msg_id="m1")
    await _deliver("subscription.on_hold", ws, msg_id="m2")

    assert (await _kinds("u-owner")).count(FAILED_KIND) == 1


async def test_the_grace_sweep_tells_admins_the_plan_moved_to_free(mongo_db, admins):
    ws = await _make_workspace()
    await Subscription(
        workspace=ws,
        gateway="dodo",
        gateway_subscription_id=SUB_ID,
        plan_key="pro",
        product_id="prod_pro_recurring",
        status="on_hold",
        grace_until=datetime.now(UTC) - timedelta(minutes=1),
    ).insert()

    assert await billing.sweep_subscription_grace() == 1

    for user in ADMINS:
        assert DOWNGRADED_KIND in await _kinds(user), user


@pytest.mark.parametrize("event_type", ["subscription.expired", "subscription.failed"])
async def test_a_dead_subscription_tells_admins_the_plan_moved_to_free(
    mongo_db, admins, event_type
):
    ws = await _make_workspace()
    await _deliver(event_type, ws, msg_id="m1")

    assert DOWNGRADED_KIND in await _kinds("u-owner")


async def test_a_broken_alert_never_blocks_the_billing_change(mongo_db, monkeypatch):
    async def _boom(workspace_id: str) -> list[str]:
        raise RuntimeError("notification path down")

    monkeypatch.setattr(workspace_service, "list_admin_ids", _boom)
    ws = await _make_workspace()

    result = await _deliver("subscription.on_hold", ws, msg_id="m1")

    assert result["ok"] is True
    row = await Subscription.find_one(Subscription.workspace == ws)
    assert row is not None and row.status == "on_hold" and row.grace_until is not None
