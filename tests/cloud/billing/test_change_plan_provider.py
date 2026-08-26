# tests/cloud/billing/test_change_plan_provider.py — proves the DodoProvider's
# change_plan wrapper sends the parameters that make an in-place plan move safe.
#
# change_plan exists so a paying site can move tiers without cancel-then-create.
# The two SDK arguments that make that true are easy to get wrong and impossible
# to notice afterwards, because the SDK returns None either way and the harm shows
# up on a customer's card rather than in a log:
#
#   * ``proration_billing_mode`` — "difference_immediately" charges only the delta
#     for the remainder of the term. "full_immediately" bills a whole new term and
#     the buyer silently forfeits the one they already paid for. Both succeed.
#   * ``on_payment_failure`` — "prevent_change" leaves a declined card on the OLD
#     plan, which is the only setting that keeps the gateway and our persisted
#     plan_tier in agreement (the caller writes the new tier on return, and the
#     resulting plan_changed webhook is acked without action, so nothing reconciles
#     a disagreement later).
#
# Created 2026-08-22 (fix/site-republish-double-billing): new test module.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider

SUB_ID = "sub_real_gateway_id"


def _provider() -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=None,
        credit_product_id="prod_credits_sku",
        plan_products={},
    )


def _fake_sdk(monkeypatch) -> MagicMock:
    """Swap the async Dodo client for a mock so nothing touches the network."""
    client = MagicMock()
    client.subscriptions.change_plan = AsyncMock(return_value=None)

    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=client))
    return client


async def test_the_plan_change_prorates_the_difference(monkeypatch):
    """Only the delta for the remaining term, never a fresh full term."""
    client = _fake_sdk(monkeypatch)

    await _provider().change_plan(
        subscription_id=SUB_ID, product_id="prod_site_business", plan_key="business", addons=[]
    )

    args, kwargs = client.subscriptions.change_plan.call_args
    assert args[0] == SUB_ID
    assert kwargs["product_id"] == "prod_site_business"
    assert kwargs["proration_billing_mode"] == "difference_immediately", (
        "a full re-bill makes the buyer forfeit the term they already paid for — "
        "the whole reason this exists instead of cancel-then-create"
    )
    # Required by the SDK even for a plain one-seat plan; omitting it raises.
    assert kwargs["quantity"] == 1


async def test_a_declined_card_leaves_the_plan_where_it_was(monkeypatch):
    """The caller persists the new tier when this returns. If a declined card
    still moved the plan at the gateway, our record and theirs would disagree with
    nothing to reconcile them — the plan_changed webhook is acked without action."""
    client = _fake_sdk(monkeypatch)

    await _provider().change_plan(
        subscription_id=SUB_ID, product_id="prod_site_business", plan_key="business", addons=[]
    )

    _, kwargs = client.subscriptions.change_plan.call_args
    assert kwargs["on_payment_failure"] == "prevent_change"


async def test_an_empty_subscription_id_never_reaches_the_gateway(monkeypatch):
    """A missing id means the caller had only a checkout session, or nothing at
    all. Fail here with a named error rather than let the gateway reject it."""
    client = _fake_sdk(monkeypatch)

    with pytest.raises(ValidationError):
        await _provider().change_plan(
            subscription_id="", product_id="prod_site_business", plan_key="business", addons=[]
        )

    client.subscriptions.change_plan.assert_not_called()


async def test_an_unconfigured_product_never_reaches_the_gateway(monkeypatch):
    """Mirrors create_subscription: an unconfigured tier is a configuration error
    with a name, not an opaque gateway 4xx."""
    client = _fake_sdk(monkeypatch)

    with pytest.raises(ValidationError):
        await _provider().change_plan(
            subscription_id=SUB_ID, product_id="", plan_key="business", addons=[]
        )

    client.subscriptions.change_plan.assert_not_called()
