# tests/cloud/billing/test_subscriptions.py — proves the BC-7 Subscription-
# primitive contract end-to-end:
#   1. ``subscribe(ws, "pro")`` opens a Dodo recurring checkout with the
#      right product id + {workspace_id, plan_key} metadata, returning the hosted
#      url (the SDK client mocked).
#   2. a VERIFIED ``subscription.active`` grants the tier's monthly allotment AND
#      upgrades ``Workspace.plan`` — entitlements then resolve to that tier.
# Updated 2026-06-25 (feat/consumer-plan-ladder): rekeyed the tiers exercised here
#   from {team, business} to the consumer ladder {go, pro}. The product map is now
#   {go: prod_go_recurring, pro: prod_pro_recurring}; the active/renew/cancel flow
#   subscribes a workspace to ``pro`` (the business-tier successor).
#   3. a VERIFIED ``subscription.renewed`` (a NEW event id) grants the allotment
#      AGAIN, ADDITIVELY — proving ROLLOVER (balance == 2x allotment after
#      active + renewed).
#   4. a REPLAY of the same renewal event id is a no-op (balance unchanged).
#   5. a VERIFIED ``subscription.cancelled`` reverts ``Workspace.plan`` to free
#      WITHOUT clawing back the granted credits.
#
# Signing uses the REAL ``standardwebhooks`` library so the verification path is
# exercised end-to-end (no shortcut around the signature check). The Dodo SDK
# CLIENT is mocked only for the ``subscribe`` checkout call; the webhook path
# never touches the SDK (verification is pure ``standardwebhooks``).
#
# Uses the shared ``mongo_db`` fixture (mongomock-motor + Beanie over
# ALL_DOCUMENTS) from tests/cloud/conftest.py. Workspaces are inserted as REAL
# ``Workspace`` docs so ``set_workspace_plan`` / ``get_workspace_plan`` (which
# parse a PydanticObjectId) and the entitlements resolver run against live docs
# — the credit wallet is keyed on the workspace id string the same way.
#
# Created 2026-06-24 (integration/billing-credits, BC-7): new test module.
# Updated 2026-06-24 (B2 review fix): added
#   test_empty_subscription_id_does_not_corrupt_cross_tenant — two workspaces whose
#   verified active events both carry an EMPTY subscription_id must NOT collide on
#   the unique (gateway, "") Subscription index. The fix SKIPS the audit upsert on
#   a falsy subscription_id, so the second workspace never overwrites the first's
#   row; the grant + plan change still apply for both.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud.billing import plans
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.entitlements import service as entitlements
from pocketpaw_ee.cloud.models.subscription import Subscription
from standardwebhooks import Webhook

# A valid Standard-Webhooks secret: ``whsec_`` + base64 of a 32-byte key.
SECRET = "whsec_" + base64.b64encode(b"billing-test-secret-key-32bytes!").decode()
# The plan -> Dodo recurring product mapping the provider/service resolve against.
PLAN_PRODUCTS = {"go": "prod_go_recurring", "pro": "prod_pro_recurring"}
SUB_ID = "sub_dodo_abc123"


def _provider() -> DodoProvider:
    """A DodoProvider wired with the test webhook secret + plan->product map.

    The API key is set so ``create_subscription`` passes its pre-flight; the SDK
    client is mocked per-test where the checkout is exercised. ``plan_products``
    feeds the webhook-parse reverse map (product_id -> plan_key fallback).
    """
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id="prod_credits_sku",
        plan_products=PLAN_PRODUCTS,
    )


@pytest.fixture
def patch_plan_products(monkeypatch: pytest.MonkeyPatch):
    """Point the plan catalog's product resolver at PLAN_PRODUCTS.

    The catalog reads ``settings.dodo_plan_products`` to populate each tier's
    ``dodo_product_id``; ``subscribe`` resolves the product through that. Patch
    the catalog helper directly so the test doesn't depend on a real settings
    load (and so ``get_plan('business').dodo_product_id`` is populated).
    """
    monkeypatch.setattr(plans, "_dodo_product_for", lambda key: PLAN_PRODUCTS.get(key))


async def _make_workspace(plan: str = "free") -> str:
    """Insert a real Workspace doc and return its id string (the wallet key)."""
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(name="Acme", slug=f"acme-{plan}", owner="u-owner", plan=plan)
    await ws.insert()
    return str(ws.id)


def _sign(body: str, *, msg_id: str, ts: datetime | None = None) -> dict[str, str]:
    """Produce valid Standard-Webhooks headers for ``body`` using ``SECRET``."""
    ts = ts or datetime.now(UTC)
    sig = Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body)  # 'v1,<b64>'
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": sig,
    }


def _subscription_body(
    *,
    event_type: str,
    workspace_id: str,
    plan_key: str = "pro",
    product_id: str = "prod_pro_recurring",
    subscription_id: str = SUB_ID,
) -> str:
    """A Dodo ``subscription.*`` webhook body (the real envelope shape).

    The buyer metadata lives at ``data.metadata`` (carrying workspace_id +
    plan_key, as ``create_subscription`` stamps), the recurring product at
    ``data.product_id``, and the subscription id at ``data.subscription_id``.
    """
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


# ---------------------------------------------------------------------------
# Criterion 1 — subscribe() opens a Dodo recurring checkout with the right
# product id + {workspace_id, plan_key} metadata, returning the hosted url.
# ---------------------------------------------------------------------------


async def test_subscribe_creates_dodo_subscription_with_product_and_metadata(
    mongo_db, monkeypatch, patch_plan_products
):
    ws = await _make_workspace(plan="free")

    # Mock the async DodoPayments client so no network call happens.
    fake_response = MagicMock()
    fake_response.payment_link = "https://checkout.dodopayments.test/sub/abc123"
    fake_response.subscription_id = SUB_ID

    fake_client = MagicMock()
    fake_client.subscriptions.create = AsyncMock(return_value=fake_response)

    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=fake_client))

    result = await billing.subscribe(
        workspace_id=ws, user_id="u1", plan_key="pro", provider=_provider()
    )
    assert result == {"checkout_url": "https://checkout.dodopayments.test/sub/abc123"}

    # The Dodo client was called with the business recurring product id and the
    # workspace_id + plan_key on metadata (so the renewal webhook can route back).
    _, kwargs = fake_client.subscriptions.create.call_args
    assert kwargs["product_id"] == "prod_pro_recurring"
    assert kwargs["payment_link"] is True
    assert kwargs["metadata"]["workspace_id"] == ws
    assert kwargs["metadata"]["plan_key"] == "pro"
    # H1 — the new-customer object MUST carry a non-empty ``name`` alongside
    # ``email``. Dodo's CustomerRequest rejects ``{email}`` alone (NewCustomer
    # requires a name); reverting _customer_param to ``{email}`` must fail here.
    customer = kwargs["customer"]
    assert customer.get("email"), customer
    assert customer.get("name"), (
        "Dodo new-customer object is missing a non-empty 'name' — "
        f"{customer!r} would be rejected server-side"
    )


async def test_subscribe_rejects_unknown_plan(mongo_db, patch_plan_products):
    ws = await _make_workspace()
    from pocketpaw_ee.cloud._core.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        await billing.subscribe(
            workspace_id=ws, user_id="u1", plan_key="platinum", provider=_provider()
        )
    assert exc.value.code == "billing.unknown_plan"


async def test_subscribe_rejects_plan_with_no_product_configured(mongo_db, monkeypatch):
    """A real tier with no Dodo product configured fails loudly (not silently)."""
    # No plan->product mapping configured: dodo_product_id resolves to None.
    monkeypatch.setattr(plans, "_dodo_product_for", lambda key: None)
    ws = await _make_workspace()
    from pocketpaw_ee.cloud._core.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        await billing.subscribe(workspace_id=ws, user_id="u1", plan_key="pro", provider=_provider())
    assert exc.value.code == "billing.plan_product_unconfigured"


# ---------------------------------------------------------------------------
# Criterion 2 — a verified subscription.active grants the business allotment AND
# upgrades Workspace.plan; entitlements then resolve to business.
# ---------------------------------------------------------------------------


async def test_active_grants_allotment_and_upgrades_plan(mongo_db):
    ws = await _make_workspace(plan="free")
    allotment = plans.get_plan("pro").monthly_credit_allotment
    assert allotment > 0

    assert await credits.balance(ws) == 0

    body = _subscription_body(event_type="subscription.active", workspace_id=ws)
    headers = _sign(body, msg_id="evt_sub_active_1")
    result = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )

    assert result == {"ok": True, "granted": True}
    # The business monthly allotment was granted to the wallet.
    assert await credits.balance(ws) == allotment

    # Workspace.plan was upgraded to business, so entitlements resolve to business.
    ent = await entitlements.resolve_entitlements(ws)
    assert ent.plan == "pro"
    assert ent.monthly_credit_allotment == allotment

    # A Subscription record was written tracking the gateway lifecycle.
    subs = await Subscription.find(Subscription.workspace == ws).to_list()
    assert len(subs) == 1
    assert subs[0].gateway == "dodo"
    assert subs[0].gateway_subscription_id == SUB_ID
    assert subs[0].plan_key == "pro"
    assert subs[0].status == "active"


async def test_active_emits_subscription_granted_event(mongo_db, recording_bus):
    ws = await _make_workspace(plan="free")
    body = _subscription_body(event_type="subscription.active", workspace_id=ws)
    headers = _sign(body, msg_id="evt_sub_active_emit")

    await billing.handle_webhook(payload=body.encode(), headers=headers, provider=_provider())

    captured = [e for e in recording_bus.events if e.type == "billing.subscription.granted"]
    assert len(captured) == 1
    data = captured[0].data
    assert data["workspace_id"] == ws
    assert data["plan_key"] == "pro"
    assert data["subscription_id"] == SUB_ID
    assert data["amount_credits"] == plans.get_plan("pro").monthly_credit_allotment


# ---------------------------------------------------------------------------
# Criterion 3 — a verified subscription.renewed (NEW event id) grants the
# allotment AGAIN additively. balance == 2x allotment after active + renewed
# (ROLLOVER — unused credits carry forward, the grant is additive).
# ---------------------------------------------------------------------------


async def test_renewal_grants_additively_proving_rollover(mongo_db):
    ws = await _make_workspace(plan="free")
    allotment = plans.get_plan("pro").monthly_credit_allotment

    # Month 1 — activation grants once.
    active_body = _subscription_body(event_type="subscription.active", workspace_id=ws)
    await billing.handle_webhook(
        payload=active_body.encode(),
        headers=_sign(active_body, msg_id="evt_active_rollover"),
        provider=_provider(),
    )
    assert await credits.balance(ws) == allotment

    # Month 2 — a renewal with a FRESH event id grants the allotment AGAIN. None
    # of month 1's credits were spent, so they ROLL OVER: balance == 2x allotment.
    renewed_body = _subscription_body(event_type="subscription.renewed", workspace_id=ws)
    result = await billing.handle_webhook(
        payload=renewed_body.encode(),
        headers=_sign(renewed_body, msg_id="evt_renewed_rollover"),  # NEW event id
        provider=_provider(),
    )

    assert result == {"ok": True, "granted": True}
    assert await credits.balance(ws) == 2 * allotment  # ROLLOVER proven


# ---------------------------------------------------------------------------
# Criterion 4 — a replay of the SAME renewal event id is a no-op (balance
# unchanged), via BC-1's unique (workspace, idempotency_key) index.
# ---------------------------------------------------------------------------


async def test_replay_same_renewal_event_id_is_noop(mongo_db, recording_bus):
    ws = await _make_workspace(plan="free")
    allotment = plans.get_plan("pro").monthly_credit_allotment

    # First activation grants.
    active_body = _subscription_body(event_type="subscription.active", workspace_id=ws)
    await billing.handle_webhook(
        payload=active_body.encode(),
        headers=_sign(active_body, msg_id="evt_active_replay"),
        provider=_provider(),
    )
    assert await credits.balance(ws) == allotment

    # A renewal grants again (fresh id) — balance is 2x.
    renewed_body = _subscription_body(event_type="subscription.renewed", workspace_id=ws)
    first = await billing.handle_webhook(
        payload=renewed_body.encode(),
        headers=_sign(renewed_body, msg_id="evt_renewed_dup_id"),
        provider=_provider(),
    )
    assert first["granted"] is True
    assert await credits.balance(ws) == 2 * allotment

    # Re-deliver the SAME renewal (same webhook-id, re-signed) — a no-op grant.
    second = await billing.handle_webhook(
        payload=renewed_body.encode(),
        headers=_sign(renewed_body, msg_id="evt_renewed_dup_id"),  # SAME id
        provider=_provider(),
    )
    assert second == {"ok": True, "granted": False}  # replay no-op
    assert await credits.balance(ws) == 2 * allotment  # unchanged — no triple-grant

    # Exactly two grant events emitted (active + the one renewal; the replay did
    # not re-emit).
    captured = [e for e in recording_bus.events if e.type == "billing.subscription.granted"]
    assert len(captured) == 2


# ---------------------------------------------------------------------------
# Criterion 5 — a verified subscription.cancelled reverts Workspace.plan to free
# WITHOUT clawing back the granted credits.
# ---------------------------------------------------------------------------


async def test_cancellation_reverts_plan_without_clawing_back_credits(mongo_db):
    ws = await _make_workspace(plan="free")
    allotment = plans.get_plan("pro").monthly_credit_allotment

    # Activate: plan -> business, allotment granted.
    active_body = _subscription_body(event_type="subscription.active", workspace_id=ws)
    await billing.handle_webhook(
        payload=active_body.encode(),
        headers=_sign(active_body, msg_id="evt_active_cancel"),
        provider=_provider(),
    )
    assert await credits.balance(ws) == allotment
    assert (await entitlements.resolve_entitlements(ws)).plan == "pro"

    # Cancel: plan reverts to free; the granted credits are NOT clawed back.
    cancel_body = _subscription_body(event_type="subscription.cancelled", workspace_id=ws)
    result = await billing.handle_webhook(
        payload=cancel_body.encode(),
        headers=_sign(cancel_body, msg_id="evt_cancelled_1"),
        provider=_provider(),
    )

    assert result == {"ok": True, "granted": False}  # cancellation never grants
    # Plan reverted to free.
    ent = await entitlements.resolve_entitlements(ws)
    assert ent.plan == "free"
    # Credits are PRESERVED — the workspace keeps what it paid for (no clawback).
    assert await credits.balance(ws) == allotment

    # The Subscription record reflects the cancelled status.
    sub = await Subscription.find_one(
        Subscription.gateway == "dodo", Subscription.gateway_subscription_id == SUB_ID
    )
    assert sub is not None
    assert sub.status == "cancelled"


# ---------------------------------------------------------------------------
# Provider-level checks — webhook parse normalizes a subscription.* delivery to
# a SubscriptionEvent, and the product_id reverse-map fills a missing plan_key.
# ---------------------------------------------------------------------------


async def test_provider_parses_subscription_event_fields():
    body = _subscription_body(
        event_type="subscription.active", workspace_id="ws_parse", plan_key="go"
    )
    headers = _sign(body, msg_id="evt_parse_sub_1")
    event = _provider().verify_and_parse_webhook(payload=body.encode(), headers=headers)
    # A subscription delivery normalizes to a SubscriptionEvent, not a GatewayEvent.
    from pocketpaw_ee.cloud.billing.domain import SubscriptionEvent

    assert isinstance(event, SubscriptionEvent)
    assert event.event_id == "evt_parse_sub_1"
    assert event.type == "subscription.active"
    assert event.workspace_id == "ws_parse"
    assert event.plan_key == "go"
    assert event.subscription_id == SUB_ID


async def test_provider_reverse_maps_product_id_when_plan_key_missing():
    """A subscription delivery whose metadata lacks plan_key still routes by the
    recurring product id (reverse-mapped through the configured plan->product)."""
    body = json.dumps(
        {
            "business_id": "biz_1",
            "type": "subscription.renewed",
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "subscription_id": SUB_ID,
                "product_id": "prod_pro_recurring",
                "metadata": {"workspace_id": "ws_rev"},  # NO plan_key
            },
        }
    )
    headers = _sign(body, msg_id="evt_reverse_1")
    event = _provider().verify_and_parse_webhook(payload=body.encode(), headers=headers)
    assert event.plan_key == "pro"  # recovered from product_id reverse-map


# ---------------------------------------------------------------------------
# B2 review fix — an empty subscription_id must NOT collide two workspaces on the
# unique (gateway, gateway_subscription_id) Subscription index. The fix skips the
# audit upsert on a falsy subscription_id; the grant + plan change still apply.
# ---------------------------------------------------------------------------


async def test_empty_subscription_id_does_not_corrupt_cross_tenant(mongo_db):
    # Distinct starting plans → distinct workspace slugs (the helper derives the
    # slug from the plan, and slug is uniquely indexed). Both are below pro,
    # so the active event upgrades each to pro.
    ws_a = await _make_workspace(plan="free")
    ws_b = await _make_workspace(plan="go")
    allotment = plans.get_plan("pro").monthly_credit_allotment
    assert allotment > 0

    # Two DIFFERENT workspaces each get a verified active event carrying an EMPTY
    # subscription_id (the gateway didn't send one). Before the fix the second
    # would overwrite the first's Subscription row via the (dodo, "") unique key.
    body_a = _subscription_body(
        event_type="subscription.active", workspace_id=ws_a, subscription_id=""
    )
    res_a = await billing.handle_webhook(
        payload=body_a.encode(),
        headers=_sign(body_a, msg_id="evt_empty_sub_a"),
        provider=_provider(),
    )
    body_b = _subscription_body(
        event_type="subscription.active", workspace_id=ws_b, subscription_id=""
    )
    res_b = await billing.handle_webhook(
        payload=body_b.encode(),
        headers=_sign(body_b, msg_id="evt_empty_sub_b"),
        provider=_provider(),
    )

    # The grant + plan change still happen for BOTH (the money path is unaffected).
    assert res_a == {"ok": True, "granted": True}
    assert res_b == {"ok": True, "granted": True}
    assert await credits.balance(ws_a) == allotment
    assert await credits.balance(ws_b) == allotment
    assert (await entitlements.resolve_entitlements(ws_a)).plan == "pro"
    assert (await entitlements.resolve_entitlements(ws_b)).plan == "pro"

    # And NO Subscription audit row was written for either (the upsert is skipped
    # on a falsy id), so there is no cross-tenant overwrite — workspace A's audit
    # trail is never replaced by workspace B's.
    subs = await Subscription.find().to_list()
    assert subs == []
