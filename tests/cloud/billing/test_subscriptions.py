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
# Updated 2026-06-28 (fix/billing-checkout-sessions): added the checkout-sessions
#   bug-fix tests — create_subscription must open a Dodo CHECKOUT SESSION with a
#   non-empty return_url + cancel_url + a product_cart line + customer (so the buyer
#   is returned to the app after paying), omit return_url when no base is available,
#   and the service must thread an ``origin`` into the return_url. Rewired the
#   existing criterion-1 mock from subscriptions.create to checkout_sessions.create.
# Updated 2026-07-08 (feat/billing-cancel-downgrade): added the two money-management
#   bug-fix tests. (A) CANCEL — ``billing.cancel`` calls the provider's
#   cancel_subscription with the ACTIVE sub id (proven at the SDK edge:
#   subscriptions.update(<id>, status="cancelled")), 402s
#   ``billing.no_active_subscription`` when there is none, and NEVER selects a stale
#   cancelled historical row; plus POST /billing/cancel route wiring (200 + 402). (B)
#   DOWNGRADE — ``subscribe`` on a workspace with an active sub does cancel-then-create
#   (opens exactly one new checkout AND cancels the old sub id), does NOT cancel when
#   there's no active sub (or only a cancelled row), and stays event_id-idempotent on
#   the new subscription's active-webhook replay.

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

    # Mock the async DodoPayments client so no network call happens. The
    # subscription checkout now goes through the CHECKOUT SESSIONS API (the fix),
    # whose response carries ``session_id`` + ``checkout_url``.
    fake_session = MagicMock()
    fake_session.checkout_url = "https://checkout.dodopayments.test/sub/abc123"
    fake_session.session_id = SESSION_ID

    fake_client = MagicMock()
    fake_client.checkout_sessions.create = AsyncMock(return_value=fake_session)

    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=fake_client))

    result = await billing.subscribe(
        workspace_id=ws, user_id="u1", plan_key="pro", provider=_provider()
    )
    assert result == {"checkout_url": "https://checkout.dodopayments.test/sub/abc123"}

    # The Dodo client was called via checkout_sessions.create with the business
    # recurring product as a product_cart line and the workspace_id + plan_key on
    # metadata (so the renewal webhook can route the grant back).
    _, kwargs = fake_client.checkout_sessions.create.call_args
    assert kwargs["product_cart"] == [{"product_id": "prod_pro_recurring", "quantity": 1}]
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


# ---------------------------------------------------------------------------
# Checkout-sessions bug fix (fix/billing-checkout-sessions) — after paying on
# Dodo the buyer must be returned to the app. The OLD path called
# ``subscriptions.create(payment_link=True)`` which accepts NO return_url, so the
# buyer was stranded. The fix moves to the Checkout Sessions API
# (``checkout_sessions.create``) which DOES carry return_url + cancel_url.
#
# These tests assert the provider now calls ``checkout_sessions.create`` (NOT
# ``subscriptions.create``) with a non-empty ``return_url``, the recurring
# product as a ``product_cart`` line ([{product_id, quantity:1}]), and a
# ``customer`` object — and that the SESSION url + session_id come back on the
# normalized ``SubscriptionCheckout``.
# ---------------------------------------------------------------------------

SESSION_ID = "cks_dodo_session_xyz789"
SESSION_CHECKOUT_URL = "https://checkout.dodopayments.com/session/cks_dodo_session_xyz789"


def _fake_session_client(monkeypatch) -> MagicMock:
    """Patch ``AsyncDodoPayments`` with a client whose ``checkout_sessions.create``
    returns a ``CheckoutSessionResponse``-shaped object (session_id + checkout_url).

    ``subscriptions.create`` is ALSO stubbed and asserted NOT-called, so a
    regression back to the stranded ``subscriptions.create(payment_link=True)``
    path fails loudly here.
    """
    fake_session = MagicMock()
    fake_session.session_id = SESSION_ID
    fake_session.checkout_url = SESSION_CHECKOUT_URL

    fake_client = MagicMock()
    fake_client.checkout_sessions.create = AsyncMock(return_value=fake_session)
    fake_client.subscriptions.create = AsyncMock(
        side_effect=AssertionError("must use checkout_sessions.create, not subscriptions.create")
    )

    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=fake_client))
    return fake_client


async def test_create_subscription_uses_checkout_session_with_return_url(monkeypatch):
    """The provider opens a CHECKOUT SESSION carrying return_url + cancel_url + a
    product_cart line + customer — so the buyer is returned to the app after pay."""
    fake_client = _fake_session_client(monkeypatch)

    checkout = await _provider().create_subscription(
        plan_key="pro",
        product_id="prod_pro_recurring",
        workspace_id="ws_checkout",
        customer_email="buyer@example.com",
        metadata={"workspace_id": "ws_checkout", "plan_key": "pro"},
        return_url="https://app.pocketpaw.test/settings/billing?checkout=success",
        cancel_url="https://app.pocketpaw.test/settings/billing?checkout=cancel",
    )

    # The Checkout Sessions API was used (NOT subscriptions.create).
    fake_client.checkout_sessions.create.assert_awaited_once()
    fake_client.subscriptions.create.assert_not_called()

    _, kwargs = fake_client.checkout_sessions.create.call_args
    # return_url is the whole point of the fix — it MUST be present and non-empty.
    assert kwargs.get("return_url"), f"return_url missing/empty: {kwargs!r}"
    assert kwargs["return_url"].startswith("https://app.pocketpaw.test/settings/billing")
    assert kwargs.get("cancel_url"), f"cancel_url missing/empty: {kwargs!r}"
    # The recurring product rides as a product_cart line (a session creates the
    # subscription at payment when the product is recurring).
    assert kwargs["product_cart"] == [{"product_id": "prod_pro_recurring", "quantity": 1}]
    # Customer object carries email + a derived name (Dodo's NewCustomer needs both).
    customer = kwargs["customer"]
    assert customer.get("email") == "buyer@example.com"
    assert customer.get("name"), customer
    # workspace_id + plan_key still ride on metadata for the renewal webhook.
    assert kwargs["metadata"]["workspace_id"] == "ws_checkout"
    assert kwargs["metadata"]["plan_key"] == "pro"

    # The normalized result carries the SESSION url + the session_id (repurposed
    # onto SubscriptionCheckout.subscription_id — the real subscription_id arrives
    # later on the subscription.active webhook body, not from this create call).
    assert checkout.checkout_url == SESSION_CHECKOUT_URL
    assert checkout.subscription_id == SESSION_ID


async def test_create_subscription_omits_return_url_when_not_provided(monkeypatch):
    """When no return base is available the call OMITS return_url (rather than
    sending an empty string) — the sites publish path passes no origin."""
    fake_client = _fake_session_client(monkeypatch)

    await _provider().create_subscription(
        plan_key="pro",
        product_id="prod_pro_recurring",
        workspace_id="ws_no_origin",
        customer_email=None,
        metadata={"workspace_id": "ws_no_origin", "plan_key": "pro"},
        # no return_url / cancel_url
    )

    _, kwargs = fake_client.checkout_sessions.create.call_args
    assert "return_url" not in kwargs
    assert "cancel_url" not in kwargs
    # Still a valid session create with the cart + customer.
    assert kwargs["product_cart"] == [{"product_id": "prod_pro_recurring", "quantity": 1}]


async def test_subscribe_threads_origin_into_return_url(mongo_db, monkeypatch, patch_plan_products):
    """End-to-end at the service layer: an ``origin`` passed to ``subscribe`` is
    woven into return_url/cancel_url on the checkout session (the route reads it
    from the buyer's Origin header)."""
    ws = await _make_workspace(plan="free")
    fake_client = _fake_session_client(monkeypatch)

    result = await billing.subscribe(
        workspace_id=ws,
        user_id="u1",
        plan_key="pro",
        origin="https://app.acme.com",
        provider=_provider(),
    )
    # The /subscribe contract is preserved — the SESSION url is returned as
    # checkout_url (the frontend reads checkout_url).
    assert result == {"checkout_url": SESSION_CHECKOUT_URL}

    _, kwargs = fake_client.checkout_sessions.create.call_args
    assert kwargs["return_url"] == "https://app.acme.com/settings/billing?checkout=success"
    assert kwargs["cancel_url"] == "https://app.acme.com/settings/billing?checkout=cancel"


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


# ---------------------------------------------------------------------------
# feat/billing-cancel-downgrade — two money-management bug fixes.
#
# Fixtures / helpers here spy at the SDK-client EDGE (a REAL DodoProvider with a
# mocked ``AsyncDodoPayments``), so the tests exercise the real provider paths:
#   * cancel  -> DodoProvider.cancel_subscription -> subscriptions.update(<id>,
#     status="cancelled")
#   * switch  -> DodoProvider.create_subscription -> checkout_sessions.create
#     AND DodoProvider.cancel_subscription -> subscriptions.update
# ---------------------------------------------------------------------------

import pytest_asyncio  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


async def _seed_subscription(
    workspace_id: str, *, subscription_id: str, status: str, plan_key: str = "pro"
) -> Subscription:
    """Insert a Subscription row directly (bypassing the webhook) to set up the
    active / cancelled fixtures the cancel + switch paths select against."""
    doc = Subscription(
        workspace=workspace_id,
        gateway="dodo",
        gateway_subscription_id=subscription_id,
        plan_key=plan_key,
        product_id="prod_pro_recurring",
        status=status,
    )
    await doc.insert()
    return doc


def _fake_switch_client(monkeypatch) -> MagicMock:
    """SDK client mock for the cancel / plan-switch paths.

    Exposes BOTH ``checkout_sessions.create`` (the new checkout) and
    ``subscriptions.update`` (how the provider cancels — a status PATCH). A
    regression back to ``subscriptions.create`` (the stranded path) still fails
    loudly via the AssertionError side effect.
    """
    fake_session = MagicMock()
    fake_session.session_id = SESSION_ID
    fake_session.checkout_url = SESSION_CHECKOUT_URL

    fake_client = MagicMock()
    fake_client.checkout_sessions.create = AsyncMock(return_value=fake_session)
    fake_client.subscriptions.update = AsyncMock(return_value=MagicMock())
    fake_client.subscriptions.create = AsyncMock(
        side_effect=AssertionError("must use checkout_sessions.create, not subscriptions.create")
    )

    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=fake_client))
    return fake_client


# ---- (A) cancel() -------------------------------------------------------- #


async def test_cancel_calls_provider_with_active_subscription_id(mongo_db, monkeypatch):
    """cancel() loads the ACTIVE sub and tells the gateway to stop billing IT."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, subscription_id=SUB_ID, status="active")
    fake_client = _fake_switch_client(monkeypatch)

    result = await billing.cancel(workspace_id=ws, provider=_provider())

    assert result == {"ok": True}
    # The real provider path cancels via the status PATCH on the ACTIVE sub id.
    fake_client.subscriptions.update.assert_awaited_once_with(SUB_ID, status="cancelled")


async def test_cancel_without_active_subscription_raises_402(mongo_db, monkeypatch):
    """No subscription at all -> 402 billing.no_active_subscription, gateway untouched."""
    ws = await _make_workspace(plan="free")
    fake_client = _fake_switch_client(monkeypatch)
    from pocketpaw_ee.cloud._core.errors import NoActiveSubscription

    with pytest.raises(NoActiveSubscription) as exc:
        await billing.cancel(workspace_id=ws, provider=_provider())
    assert exc.value.status_code == 402
    assert exc.value.code == "billing.no_active_subscription"
    fake_client.subscriptions.update.assert_not_called()


async def test_cancel_ignores_stale_cancelled_row(mongo_db, monkeypatch):
    """A historical CANCELLED row must NOT be selected — cancel targets the ACTIVE
    row's id (the (workspace) index is non-unique, so a naive first-match is wrong)."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, subscription_id="sub_old_cancelled", status="cancelled")
    await _seed_subscription(ws, subscription_id="sub_current_active", status="active")
    fake_client = _fake_switch_client(monkeypatch)

    await billing.cancel(workspace_id=ws, provider=_provider())

    fake_client.subscriptions.update.assert_awaited_once_with(
        "sub_current_active", status="cancelled"
    )


async def test_cancel_with_only_cancelled_row_raises_402(mongo_db, monkeypatch):
    """Only a stale cancelled row exists -> 402 (a naive first-match would wrongly
    'cancel' the dead row; the status filter prevents that)."""
    ws = await _make_workspace(plan="free")
    await _seed_subscription(ws, subscription_id="sub_dead", status="cancelled")
    fake_client = _fake_switch_client(monkeypatch)
    from pocketpaw_ee.cloud._core.errors import NoActiveSubscription

    with pytest.raises(NoActiveSubscription):
        await billing.cancel(workspace_id=ws, provider=_provider())
    fake_client.subscriptions.update.assert_not_called()


# ---- (A) cancel() route wiring (httpx AsyncClient) ----------------------- #


@pytest_asyncio.fixture
async def billing_app_client() -> AsyncClient:
    """A FastAPI app with ONLY the billing router mounted + auth/license overridden,
    scoped to workspace ``w1`` — so the POST /billing/cancel wiring is exercised
    without a real JWT / license (mirrors the shared cloud_app_client fixture)."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.billing.router import router as billing_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(billing_router)
    app.dependency_overrides[current_user_id] = lambda: "u1"
    app.dependency_overrides[current_workspace_id] = lambda: "w1"
    app.dependency_overrides[require_license] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_cancel_route_cancels_active_subscription(mongo_db, billing_app_client, monkeypatch):
    """POST /billing/cancel -> 200 {ok: true} and the gateway sub is cancelled."""
    await _seed_subscription("w1", subscription_id=SUB_ID, status="active")
    fake_client = _fake_switch_client(monkeypatch)
    # The route uses the DEFAULT provider — point it at the test provider whose SDK
    # client is mocked (no real Dodo settings / network needed).
    monkeypatch.setattr(billing, "_default_provider", lambda: _provider())

    resp = await billing_app_client.post("/billing/cancel")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    fake_client.subscriptions.update.assert_awaited_once_with(SUB_ID, status="cancelled")


async def test_cancel_route_402_when_no_active_subscription(
    mongo_db, billing_app_client, monkeypatch
):
    """POST /billing/cancel with no active sub -> 402 billing.no_active_subscription."""
    monkeypatch.setattr(billing, "_default_provider", lambda: _provider())

    resp = await billing_app_client.post("/billing/cancel")

    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "billing.no_active_subscription"


# ---- (B) subscribe() switch guard --------------------------------------- #


async def test_subscribe_with_active_sub_cancels_old_then_creates_new(
    mongo_db, monkeypatch, patch_plan_products
):
    """A switch (workspace already has an active sub) opens exactly ONE new checkout
    AND cancels the OLD subscription — never the blind double-create."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, subscription_id=SUB_ID, status="active", plan_key="pro")
    fake_client = _fake_switch_client(monkeypatch)

    result = await billing.subscribe(
        workspace_id=ws, user_id="u1", plan_key="go", provider=_provider()
    )
    assert result == {"checkout_url": SESSION_CHECKOUT_URL}

    # Exactly one NEW checkout was opened (no second parallel subscribe)...
    fake_client.checkout_sessions.create.assert_awaited_once()
    # ...and the OLD subscription was cancelled at the gateway. The pre-fix blind
    # path (create WITHOUT cancel) would leave subscriptions.update un-called.
    fake_client.subscriptions.update.assert_awaited_once_with(SUB_ID, status="cancelled")


async def test_subscribe_without_active_sub_does_not_cancel(
    mongo_db, monkeypatch, patch_plan_products
):
    """A fresh subscribe (no active sub) opens the checkout and cancels NOTHING."""
    ws = await _make_workspace(plan="free")
    fake_client = _fake_switch_client(monkeypatch)

    await billing.subscribe(workspace_id=ws, user_id="u1", plan_key="pro", provider=_provider())

    fake_client.checkout_sessions.create.assert_awaited_once()
    fake_client.subscriptions.update.assert_not_called()


async def test_subscribe_ignores_cancelled_row_and_does_not_cancel(
    mongo_db, monkeypatch, patch_plan_products
):
    """A stale CANCELLED row is not 'active' — subscribe treats the workspace as
    having no active sub and cancels nothing (no spurious gateway call)."""
    ws = await _make_workspace(plan="free")
    await _seed_subscription(ws, subscription_id="sub_dead", status="cancelled")
    fake_client = _fake_switch_client(monkeypatch)

    await billing.subscribe(workspace_id=ws, user_id="u1", plan_key="pro", provider=_provider())

    fake_client.checkout_sessions.create.assert_awaited_once()
    fake_client.subscriptions.update.assert_not_called()


async def test_switch_then_new_active_webhook_is_idempotent(
    mongo_db, monkeypatch, patch_plan_products
):
    """After a switch, the NEW subscription's active webhook grants exactly once — a
    replay (same event_id) is a no-op, so the switch never breaks BC-1 replay-safety."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, subscription_id=SUB_ID, status="active", plan_key="pro")
    _fake_switch_client(monkeypatch)

    # Switch to go — opens a new checkout + cancels the old sub.
    await billing.subscribe(workspace_id=ws, user_id="u1", plan_key="go", provider=_provider())

    go_allotment = plans.get_plan("go").monthly_credit_allotment
    assert go_allotment > 0
    before = await credits.balance(ws)

    # The NEW subscription's active webhook lands (fresh gateway sub id + event id).
    body = _subscription_body(
        event_type="subscription.active",
        workspace_id=ws,
        plan_key="go",
        product_id="prod_go_recurring",
        subscription_id="sub_new_go",
    )
    first = await billing.handle_webhook(
        payload=body.encode(),
        headers=_sign(body, msg_id="evt_switch_go_active"),
        provider=_provider(),
    )
    assert first == {"ok": True, "granted": True}
    assert await credits.balance(ws) == before + go_allotment

    # Replay the SAME active event id — a no-op (BC-1 unique index); balance flat.
    second = await billing.handle_webhook(
        payload=body.encode(),
        headers=_sign(body, msg_id="evt_switch_go_active"),  # SAME id
        provider=_provider(),
    )
    assert second == {"ok": True, "granted": False}
    assert await credits.balance(ws) == before + go_allotment


# ---------------------------------------------------------------------------
# fix/cancel-webhook-revert-guard — the subscription.cancelled plan revert must
# NOT be unconditional. Under unordered / at-least-once delivery a
# ``cancelled(old_sub)`` retry can land AFTER ``active(new_sub)`` during a plan
# switch; reverting to free there strands a paying customer on free entitlements
# (nothing self-heals it — only subscription.active re-sets the plan). The fix
# marks THIS sub cancelled first, then reverts to free ONLY when no OTHER active
# subscription still owns the workspace.
# ---------------------------------------------------------------------------


async def test_cancelled_does_not_revert_when_newer_active_sub_exists(mongo_db):
    """Reordered delivery: active(new) already landed (workspace on pro, a NEW active
    Subscription row owns it) when a stale cancelled(old) retry arrives. The plan
    must STAY pro — the stale cancel must not downgrade the live subscription."""
    ws = await _make_workspace(plan="pro")
    # The newer subscription's active row already owns the workspace (as if its
    # subscription.active landed first in the reordered delivery).
    await _seed_subscription(ws, subscription_id="sub_new_active", status="active", plan_key="pro")
    assert (await entitlements.resolve_entitlements(ws)).plan == "pro"

    # A stale cancelled retry for a DIFFERENT (older) subscription lands now.
    cancel_body = _subscription_body(
        event_type="subscription.cancelled", workspace_id=ws, subscription_id="sub_old_cancelled"
    )
    result = await billing.handle_webhook(
        payload=cancel_body.encode(),
        headers=_sign(cancel_body, msg_id="evt_stale_cancel"),
        provider=_provider(),
    )

    assert result == {"ok": True, "granted": False}
    # The plan is NOT reverted to free — the newer active sub still owns the workspace.
    assert (await entitlements.resolve_entitlements(ws)).plan == "pro"
    # The newer subscription's row is untouched (still active).
    still = await Subscription.find_one(
        Subscription.gateway == "dodo",
        Subscription.gateway_subscription_id == "sub_new_active",
    )
    assert still is not None
    assert still.status == "active"


async def test_cancelled_reverts_to_free_when_no_other_active_sub(mongo_db):
    """The normal cancel: the ONLY subscription is the one being cancelled, so no
    other active sub remains — the plan reverts to free (credits untouched)."""
    ws = await _make_workspace(plan="pro")
    await _seed_subscription(ws, subscription_id=SUB_ID, status="active", plan_key="pro")
    # Give the wallet a balance to prove cancellation never claws it back.
    await credits.grant(workspace=ws, amount=500, cause="seed", idempotency_key="seed_cancel")
    assert await credits.balance(ws) == 500

    cancel_body = _subscription_body(
        event_type="subscription.cancelled", workspace_id=ws, subscription_id=SUB_ID
    )
    result = await billing.handle_webhook(
        payload=cancel_body.encode(),
        headers=_sign(cancel_body, msg_id="evt_solo_cancel"),
        provider=_provider(),
    )

    assert result == {"ok": True, "granted": False}
    # No other active sub -> plan reverts to free.
    assert (await entitlements.resolve_entitlements(ws)).plan == "free"
    # Credits preserved (no clawback).
    assert await credits.balance(ws) == 500
    # The cancelled subscription's row reflects the cancelled status.
    sub = await Subscription.find_one(
        Subscription.gateway == "dodo", Subscription.gateway_subscription_id == SUB_ID
    )
    assert sub is not None
    assert sub.status == "cancelled"
