# tests/cloud/billing/test_dunning.py — proves M5: a failing card now costs the
# workspace its paid entitlements, on a grace period rather than immediately,
# and WITHOUT the double-billing regression that the naive fix reintroduces.
#
# The trap this module exists to pin: ``_billable_subscription`` used to filter
# ``status == "active"``. Writing ``"on_hold"`` into that field without widening
# the predicate makes ``subscribe()``'s guard stop seeing the row, so a plan
# switch during dunning opens a SECOND parallel Dodo subscription and the buyer
# is billed twice — the exact defect fixed on 2026-07-08.
# ``test_subscribe_during_dunning_does_not_open_a_second_subscription`` fails
# without the widened predicate.
#
# The state machine under test:
#   on_hold           -> stamp grace_until, entitlements KEPT (that is grace)
#   grace expired     -> the sweep suspends: Workspace.plan -> free
#   renewed / active  -> grace cleared, entitlements restored
#   expired / failed  -> suspended immediately, no grace
#   every path        -> credits are NEVER clawed back (that is M1's job, and
#                        only for an actual refund or a lost dispute)
#
# Created 2026-09-02 (fix/billing-reversals-and-dunning, M5): new test module.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud.billing import plans
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.subscription import Subscription
from pocketpaw_ee.cloud.workspace import service as workspace_service
from standardwebhooks import Webhook

SECRET = "whsec_" + base64.b64encode(b"billing-test-secret-key-32bytes!").decode()
PLAN_PRODUCTS = {"go": "prod_go_recurring", "pro": "prod_pro_recurring"}
SUB_ID = "sub_dunning_1"
SESSION_CHECKOUT_URL = "https://checkout.dodo.test/session/cks_1"


def _provider() -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id="prod_credits_sku",
        plan_products=PLAN_PRODUCTS,
    )


@pytest.fixture
def patch_plan_products(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(plans, "_dodo_product_for", lambda key: PLAN_PRODUCTS.get(key))


def _sign(body: str, *, msg_id: str) -> dict[str, str]:
    ts = datetime.now(UTC)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body),
    }


def _subscription_body(
    *,
    event_type: str,
    workspace_id: str,
    plan_key: str = "pro",
    product_id: str = "prod_pro_recurring",
    subscription_id: str = SUB_ID,
) -> str:
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "subscription_id": subscription_id,
                "product_id": product_id,
                "metadata": {"workspace_id": workspace_id, "plan_key": plan_key},
            },
        }
    )


async def _deliver(event_type: str, workspace_id: str, *, msg_id: str, **kw) -> dict:
    body = _subscription_body(event_type=event_type, workspace_id=workspace_id, **kw)
    return await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id=msg_id), provider=_provider()
    )


async def _make_workspace(plan: str = "free", slug_suffix: str = "") -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(name="Acme", slug=f"acme-{plan}{slug_suffix}", owner="u-owner", plan=plan)
    await ws.insert()
    return str(ws.id)


async def _seed_subscription(
    workspace_id: str,
    *,
    subscription_id: str = SUB_ID,
    status: str = "active",
    plan_key: str = "pro",
    grace_until: datetime | None = None,
    suspended_at: datetime | None = None,
) -> Subscription:
    doc = Subscription(
        workspace=workspace_id,
        gateway="dodo",
        gateway_subscription_id=subscription_id,
        plan_key=plan_key,
        product_id="prod_pro_recurring",
        status=status,
        grace_until=grace_until,
        suspended_at=suspended_at,
    )
    await doc.insert()
    return doc


def _fake_switch_client(monkeypatch) -> MagicMock:
    """SDK client mock exposing checkout_sessions.create + subscriptions.update,
    mirroring ``test_subscriptions.py``."""
    fake_session = MagicMock()
    fake_session.session_id = "cks_1"
    fake_session.checkout_url = SESSION_CHECKOUT_URL

    fake_client = MagicMock()
    fake_client.checkout_sessions.create = AsyncMock(return_value=fake_session)
    fake_client.subscriptions.update = AsyncMock(return_value=MagicMock())
    fake_client.subscriptions.change_plan = AsyncMock(return_value=MagicMock())

    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=fake_client))
    return fake_client


# ---------------------------------------------------------------------------
# THE TRAP. Widening the billable predicate to {active, on_hold} is what keeps
# a plan switch during dunning from opening a second parallel subscription.
# ---------------------------------------------------------------------------


async def test_subscribe_during_dunning_does_not_open_a_second_subscription(
    mongo_db, monkeypatch, patch_plan_products
):
    """The regression guard. With ``_billable_subscription`` still filtering
    ``status == "active"`` the on-hold row is invisible, the cancel-then-create
    guard is skipped, and the buyer ends up on two Dodo subscriptions at once."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, status="on_hold", grace_until=datetime.now(UTC))
    fake_client = _fake_switch_client(monkeypatch)

    result = await billing.subscribe(
        workspace_id=ws, user_id="u1", plan_key="go", provider=_provider()
    )

    assert result == {"checkout_url": SESSION_CHECKOUT_URL}
    fake_client.checkout_sessions.create.assert_awaited_once()
    # The on-hold subscription was cancelled at the gateway BEFORE the new one
    # can start billing. Without the widened predicate this is never called.
    fake_client.subscriptions.update.assert_awaited_once_with(SUB_ID, status="cancelled")


async def test_cancel_works_on_a_subscription_in_dunning(mongo_db, monkeypatch):
    """A buyer whose card is failing must still be able to stop the retries. The
    narrow predicate answers 402 here and leaves Dodo billing them."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, status="on_hold", grace_until=datetime.now(UTC))
    fake_client = _fake_switch_client(monkeypatch)

    assert await billing.cancel(workspace_id=ws, provider=_provider()) == {"ok": True}
    fake_client.subscriptions.update.assert_awaited_once_with(SUB_ID, status="cancelled")


# REMOVED 2026-09-05 (fix/sites-plan-credits):
# ``test_site_addon_sync_targets_a_subscription_in_dunning`` asserted that a site
# add-on could still be attached while the workspace subscription was on hold.
# Site plans no longer attach to that subscription at all — they are paid from
# the workspace credit balance — and ``sync_site_addons`` is gone with the rest
# of the rail. The dunning behaviour it leaned on (an on-hold subscription is
# still the one the workspace HAS) is covered by the cancel and re-subscribe
# cases above and below.


async def test_a_suspended_subscription_is_still_cancelled_before_a_new_one_opens(
    mongo_db, monkeypatch, patch_plan_products
):
    """Suspension revokes ENTITLEMENTS; it does not end the gateway subscription,
    which is still on hold at Dodo and can still recover. So the row stays
    billable and a re-subscribe still cancels it first."""
    ws = await _make_workspace(plan="free")
    await _seed_subscription(
        ws,
        status="on_hold",
        grace_until=datetime.now(UTC) - timedelta(days=30),
        suspended_at=datetime.now(UTC),
    )
    fake_client = _fake_switch_client(monkeypatch)

    await billing.subscribe(workspace_id=ws, user_id="u1", plan_key="pro", provider=_provider())
    fake_client.subscriptions.update.assert_awaited_once_with(SUB_ID, status="cancelled")


# ---------------------------------------------------------------------------
# on_hold — stamp the grace deadline, keep entitlements, take no money action.
# ---------------------------------------------------------------------------


async def test_on_hold_stamps_a_grace_deadline_and_keeps_the_plan(mongo_db):
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, status="active")
    await credits.grant(workspace=ws, amount=7500, cause="subscription_grant", idempotency_key="g1")

    result = await _deliver("subscription.on_hold", ws, msg_id="evt_hold_1")

    assert result == {"ok": True, "granted": False}
    row = await Subscription.find_one(Subscription.workspace == ws)
    assert row is not None
    assert row.status == "on_hold"
    assert row.grace_until is not None
    assert row.suspended_at is None
    # Grace means the buyer KEEPS the plan while their card is retried...
    assert await workspace_service.get_workspace_plan(ws) == "pro"
    # ...and dunning never touches credits. That is M1's job, and only for an
    # actual refund or a lost dispute.
    assert await credits.balance(ws) == 7500


async def test_a_redelivered_on_hold_does_not_extend_the_grace_window(mongo_db):
    """At-least-once delivery must not hand a non-paying workspace a fresh grace
    period every retry."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, status="active")

    await _deliver("subscription.on_hold", ws, msg_id="evt_hold_a")
    first = (await Subscription.find_one(Subscription.workspace == ws)).grace_until

    await _deliver("subscription.on_hold", ws, msg_id="evt_hold_b")
    second = (await Subscription.find_one(Subscription.workspace == ws)).grace_until

    assert first == second


async def test_on_hold_creates_a_row_when_the_activation_was_never_seen(mongo_db):
    ws = await _make_workspace(plan="pro")
    await _deliver("subscription.on_hold", ws, msg_id="evt_hold_new")

    row = await Subscription.find_one(Subscription.workspace == ws)
    assert row is not None
    assert row.status == "on_hold"
    assert row.grace_until is not None


# ---------------------------------------------------------------------------
# Recovery — a successful retry clears the grace state and restores the plan.
# ---------------------------------------------------------------------------


async def test_renewed_clears_the_grace_state_and_restores_the_plan(mongo_db):
    ws = await _make_workspace(plan="free")
    await _seed_subscription(
        ws,
        status="on_hold",
        grace_until=datetime.now(UTC) - timedelta(days=1),
        suspended_at=datetime.now(UTC),
    )

    await _deliver("subscription.renewed", ws, msg_id="evt_renew_1")

    row = await Subscription.find_one(Subscription.workspace == ws)
    assert row is not None
    assert row.status == "active"
    assert row.grace_until is None
    assert row.suspended_at is None
    # A recovered payment must put the entitlements back, not leave the buyer on
    # free while paying for pro.
    assert await workspace_service.get_workspace_plan(ws) == "pro"


async def test_active_clears_the_grace_state(mongo_db):
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, status="on_hold", grace_until=datetime.now(UTC))

    await _deliver("subscription.active", ws, msg_id="evt_active_1")

    row = await Subscription.find_one(Subscription.workspace == ws)
    assert row is not None
    assert row.status == "active"
    assert row.grace_until is None


# ---------------------------------------------------------------------------
# expired / failed — terminal at the gateway, so suspend with no grace at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["subscription.expired", "subscription.failed"])
async def test_expired_and_failed_suspend_immediately(mongo_db, event_type):
    ws = await _make_workspace(plan="pro", slug_suffix=event_type)
    await _seed_subscription(ws, status="active")
    await credits.grant(workspace=ws, amount=7500, cause="subscription_grant", idempotency_key="g1")

    result = await _deliver(event_type, ws, msg_id=f"evt_{event_type}")

    assert result == {"ok": True, "granted": False}
    row = await Subscription.find_one(Subscription.workspace == ws)
    assert row is not None
    assert row.status == "expired"
    assert await workspace_service.get_workspace_plan(ws) == "free"
    # Still no clawback on a dunning path.
    assert await credits.balance(ws) == 7500


async def test_expiry_does_not_downgrade_a_workspace_that_has_another_subscription(mongo_db):
    """Deliveries are unordered. An ``expired`` for the OLD subscription must not
    strip the plan a newly-active one just granted."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, subscription_id="sub_old", status="active")
    await _seed_subscription(ws, subscription_id="sub_new", status="active")

    await _deliver("subscription.expired", ws, msg_id="evt_exp_old", subscription_id="sub_old")

    assert await workspace_service.get_workspace_plan(ws) == "pro"


# ---------------------------------------------------------------------------
# The sweep — what actually turns a stamped grace deadline into a suspension.
# ---------------------------------------------------------------------------


async def test_the_sweep_suspends_a_subscription_past_its_grace_deadline(mongo_db):
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(
        ws, status="on_hold", grace_until=datetime.now(UTC) - timedelta(minutes=1)
    )
    await credits.grant(workspace=ws, amount=7500, cause="subscription_grant", idempotency_key="g1")

    assert await billing.sweep_subscription_grace() == 1

    row = await Subscription.find_one(Subscription.workspace == ws)
    assert row is not None
    assert row.suspended_at is not None
    assert row.status == "on_hold"  # the gateway subscription is still on hold
    assert await workspace_service.get_workspace_plan(ws) == "free"
    assert await credits.balance(ws) == 7500  # never a clawback


async def test_the_sweep_leaves_a_subscription_inside_its_grace_window_alone(mongo_db):
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(
        ws, status="on_hold", grace_until=datetime.now(UTC) + timedelta(days=3)
    )

    assert await billing.sweep_subscription_grace() == 0
    assert await workspace_service.get_workspace_plan(ws) == "pro"


async def test_the_sweep_is_idempotent(mongo_db):
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(
        ws, status="on_hold", grace_until=datetime.now(UTC) - timedelta(days=1)
    )

    assert await billing.sweep_subscription_grace() == 1
    assert await billing.sweep_subscription_grace() == 0


async def test_the_sweep_does_not_downgrade_a_workspace_that_pays_on_another_subscription(
    mongo_db,
):
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(
        ws,
        subscription_id="sub_stale",
        status="on_hold",
        grace_until=datetime.now(UTC) - timedelta(days=1),
    )
    await _seed_subscription(ws, subscription_id="sub_live", status="active")

    assert await billing.sweep_subscription_grace() == 1
    # The stale row is marked, but the workspace keeps the plan the live
    # subscription is paying for.
    assert await workspace_service.get_workspace_plan(ws) == "pro"


async def test_the_sweep_ignores_a_subscription_with_no_grace_deadline(mongo_db):
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, status="on_hold", grace_until=None)

    assert await billing.sweep_subscription_grace() == 0
    assert await workspace_service.get_workspace_plan(ws) == "pro"
