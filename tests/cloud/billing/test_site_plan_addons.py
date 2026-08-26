# tests/cloud/billing/test_site_plan_addons.py — the ADD-ON RAIL: a paid site is
# a LINE on the workspace's existing subscription, not a subscription of its own.
#
# What these cover, and why each one is here rather than being obvious:
#
#   * The SDK edge — ``change_plan`` forwards ``addons`` ALWAYS, empty included.
#     Dodo treats the add-on list as declarative ("leaving this empty would remove
#     any existing addons"), so an OMITTED list is not "leave the cart alone", it
#     is "empty the cart". A provider that only passed the kwarg when non-empty
#     would silently cancel every paid site in a workspace the next time anyone
#     changed the workspace plan, and the only symptom would be a smaller invoice
#     a month later.
#   * The cart builder — aggregation, and the three exclusions. The double-billing
#     one is the expensive bug: a site already on a legacy per-site subscription
#     that also lands on the workspace cart is charged twice for one site.
#   * The refusal — a workspace with no subscription has nothing to attach an
#     add-on to, and must say so rather than quietly opening the separate
#     subscription this rail exists to remove.
#
# Uses the shared ``mongo_db`` fixture (mongomock-motor + Beanie over
# ALL_DOCUMENTS) from tests/cloud/conftest.py, with REAL ``Site`` and
# ``Subscription`` documents — the cart is built by querying them, so a stubbed
# repository would test the stub rather than the query.
#
# Created 2026-08-26 (feat/site-plans-as-addons): new test module.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud._core.errors import NoActiveSubscription
from pocketpaw_ee.cloud.billing import service as billing_service
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.subscription import Subscription

pytestmark = pytest.mark.anyio

WORKSPACE = "ws_addons"
ADDONS = {"site": "adn_site", "staff": "adn_staff"}


def _addons(monkeypatch, mapping: dict[str, str] | None = None) -> None:
    """Point the lazily-imported ``get_settings`` at an add-on map stub.

    ``_dodo_addon_for`` imports ``get_settings`` inside the call, so the patch has
    to land on the config module rather than on a name already bound here.
    """
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(
            dodo_site_addons=ADDONS if mapping is None else mapping,
            dodo_site_products=None,
        ),
    )


async def _site(*, tier: str, status: str = "active", subscription_id: str | None = None) -> Site:
    doc = Site(
        workspace=WORKSPACE,
        pocket_id=f"pocket_{tier}_{status}_{subscription_id or 'none'}",
        owner="user_1",
        name="a site",
        plan_tier=tier,
        subscription_status=status,
        subscription_id=subscription_id,
    )
    await doc.insert()
    return doc


async def _subscription(*, plan_key: str = "pro", product_id: str = "prod_ws_pro") -> Subscription:
    doc = Subscription(
        workspace=WORKSPACE,
        gateway="dodo",
        gateway_subscription_id="sub_workspace_1",
        plan_key=plan_key,
        product_id=product_id,
        status="active",
    )
    await doc.insert()
    return doc


# --------------------------------------------------------------------------- #
# The gateway edge
# --------------------------------------------------------------------------- #


async def test_the_provider_forwards_an_empty_cart_rather_than_omitting_it(monkeypatch):
    """An omitted add-on list EMPTIES the cart at this gateway, so the parameter
    must be sent on every call — including, and especially, when it is empty.

    If the adapter dropped the kwarg when the list was empty, the SDK would omit
    it, Dodo would read that as "remove every addon", and a workspace that changed
    its plan would lose the billing for every paid site it runs while our own
    documents still said those sites were active."""
    from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider

    client = MagicMock()
    client.subscriptions.change_plan = AsyncMock(return_value=None)
    provider = DodoProvider(
        api_key="k",
        environment="test_mode",
        webhook_secret="whsec_x",
        credit_product_id=None,
        plan_products={},
    )
    monkeypatch.setattr(provider, "_client", lambda: client)

    await provider.change_plan(
        subscription_id="sub_1", product_id="prod_ws_pro", plan_key="pro", addons=[]
    )

    _, kwargs = client.subscriptions.change_plan.call_args
    assert "addons" in kwargs, (
        "the kwarg must be present even when empty — an omitted list is not "
        "'leave the cart alone', it is 'empty the cart'"
    )
    assert kwargs["addons"] == []


async def test_the_provider_forwards_the_cart_it_was_given(monkeypatch):
    from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider

    client = MagicMock()
    client.subscriptions.change_plan = AsyncMock(return_value=None)
    provider = DodoProvider(
        api_key="k",
        environment="test_mode",
        webhook_secret="whsec_x",
        credit_product_id=None,
        plan_products={},
    )
    monkeypatch.setattr(provider, "_client", lambda: client)

    cart = [{"addon_id": "adn_site", "quantity": 3}]
    await provider.change_plan(
        subscription_id="sub_1", product_id="prod_ws_pro", plan_key="pro", addons=cart
    )

    _, kwargs = client.subscriptions.change_plan.call_args
    assert kwargs["addons"] == cart


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #


def test_a_tier_is_purchasable_on_the_addon_id_alone(monkeypatch):
    """The add-on id is the rail a new purchase takes, so it alone must make a
    tier buyable — a deployment that has configured add-ons and never configured
    the per-site products is the intended end state, not a broken one."""
    _addons(monkeypatch)
    tier = site_plans.get_site_plan("site")
    assert tier is not None
    assert tier.dodo_addon_id == "adn_site"
    assert tier.dodo_product_id is None
    assert tier.purchasable is True


def test_an_org_flat_is_still_unpurchasable_however_it_is_configured(monkeypatch):
    """Add-ons make an org flat mechanically chargeable for the first time, which
    is exactly why this stays asserted: nothing about this change should let a
    per-site publish buy a plan that covers twenty-five sites."""
    _addons(monkeypatch, {"studio": "adn_studio", "agency": "adn_agency"})
    for key in ("studio", "agency"):
        tier = site_plans.get_site_plan(key)
        assert tier is not None
        assert tier.purchasable is False, f"{key} is an org flat and must stay unbuyable here"


def test_the_legacy_tier_names_still_resolve_an_addon(monkeypatch):
    """A deployment keyed by the pre-2026-08-22 names must keep charging. The
    product map already had this alias lookup; the add-on map needs the same one
    or a rename turns every paid tier unbuyable on deploy."""
    _addons(monkeypatch, {"pro": "adn_legacy_pro"})
    tier = site_plans.get_site_plan("site")
    assert tier is not None
    assert tier.dodo_addon_id == "adn_legacy_pro"


# --------------------------------------------------------------------------- #
# The cart
# --------------------------------------------------------------------------- #


async def test_the_cart_aggregates_sites_on_the_same_tier(mongo_db, monkeypatch):  # noqa: ARG001
    """Dodo keys a cart line by add-on id, so four sites on one tier are one line
    of quantity four. Emitting the id four times is a malformed cart."""
    _addons(monkeypatch)
    for _ in range(3):
        await _site(tier="site")
    await _site(tier="staff")

    cart = await billing_service._site_addon_cart(WORKSPACE)

    assert cart == [
        {"addon_id": "adn_site", "quantity": 3},
        {"addon_id": "adn_staff", "quantity": 1},
    ]


async def test_a_site_on_a_legacy_per_site_subscription_stays_off_the_cart(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """THE DOUBLE-BILLING GUARD. Per-site subscriptions are live in production and
    Dodo is already charging for them on their own rail. Counting one on the
    workspace cart too would charge the customer twice for a single site, and the
    only place it would show up is the invoice."""
    _addons(monkeypatch)
    await _site(tier="site")
    await _site(tier="staff", subscription_id="sub_per_site_legacy")

    cart = await billing_service._site_addon_cart(WORKSPACE)

    assert cart == [{"addon_id": "adn_site", "quantity": 1}]


async def test_a_cancelled_site_drops_off_the_cart(mongo_db, monkeypatch):  # noqa: ARG001
    """This IS the cancellation mechanism under the add-on model: the cart is
    rebuilt from the documents and pushed whole, so a site that stops being active
    stops being billed on the next sync. Nothing has to remember to detach it."""
    _addons(monkeypatch)
    await _site(tier="site")
    await _site(tier="site", status="cancelled")
    await _site(tier="staff", status="none")

    cart = await billing_service._site_addon_cart(WORKSPACE)

    assert cart == [{"addon_id": "adn_site", "quantity": 1}]


async def test_a_free_site_is_never_a_cart_line(mongo_db, monkeypatch):  # noqa: ARG001
    _addons(monkeypatch)
    await _site(tier="free")

    assert await billing_service._site_addon_cart(WORKSPACE) == []


async def test_a_tier_with_no_configured_addon_is_skipped(mongo_db, monkeypatch):  # noqa: ARG001
    """Nothing is guessed for an unconfigured tier. The publish path separately
    refuses to record a tier it cannot charge for, so this only ever sees a site
    whose tier lost its configuration after the sale."""
    _addons(monkeypatch, {"site": "adn_site"})
    await _site(tier="site")
    await _site(tier="staff")

    assert await billing_service._site_addon_cart(WORKSPACE) == [
        {"addon_id": "adn_site", "quantity": 1}
    ]


async def test_the_cart_is_scoped_to_one_workspace(mongo_db, monkeypatch):  # noqa: ARG001
    _addons(monkeypatch)
    await _site(tier="site")
    other = Site(
        workspace="ws_someone_else",
        pocket_id="pocket_other",
        owner="user_2",
        name="not ours",
        plan_tier="staff",
        subscription_status="active",
    )
    await other.insert()

    assert await billing_service._site_addon_cart(WORKSPACE) == [
        {"addon_id": "adn_site", "quantity": 1}
    ]


# --------------------------------------------------------------------------- #
# The sync
# --------------------------------------------------------------------------- #


async def test_sync_refuses_a_workspace_with_no_subscription(mongo_db, monkeypatch):  # noqa: ARG001
    """An add-on attaches to something. Refusing here is the deliberate shape of
    the feature — the alternative is opening a standalone per-site subscription,
    which is the separate payment this rail replaces."""
    _addons(monkeypatch)
    await _site(tier="site")
    provider = MagicMock()
    provider.change_plan = AsyncMock()

    with pytest.raises(NoActiveSubscription):
        await billing_service.sync_site_addons(WORKSPACE, provider=provider)

    provider.change_plan.assert_not_awaited()


async def test_sync_pushes_the_full_cart_onto_the_existing_subscription(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """One subscription, one bill. The site is charged by moving the workspace's
    OWN subscription onto the same product it already has with a fuller cart —
    never by creating a second subscription."""
    _addons(monkeypatch)
    await _subscription()
    await _site(tier="site")
    await _site(tier="staff")
    provider = MagicMock()
    provider.change_plan = AsyncMock(return_value=None)
    provider.create_subscription = AsyncMock()

    result = await billing_service.sync_site_addons(WORKSPACE, provider=provider)

    _, kwargs = provider.change_plan.call_args
    assert kwargs["subscription_id"] == "sub_workspace_1"
    assert kwargs["product_id"] == "prod_ws_pro", (
        "re-send the product the subscription is already on — publishing a site "
        "must not move the workspace's plan as a side effect"
    )
    assert kwargs["addons"] == [
        {"addon_id": "adn_site", "quantity": 1},
        {"addon_id": "adn_staff", "quantity": 1},
    ]
    provider.create_subscription.assert_not_awaited()
    assert result["subscription_id"] == "sub_workspace_1"


async def test_sync_is_idempotent(mongo_db, monkeypatch):  # noqa: ARG001
    """The cart is recomputed and sent whole every time, so a second sync with no
    change between sends a byte-identical payload. Callers never have to work out
    whether a sync is 'needed'."""
    _addons(monkeypatch)
    await _subscription()
    await _site(tier="site")
    provider = MagicMock()
    provider.change_plan = AsyncMock(return_value=None)

    first = await billing_service.sync_site_addons(WORKSPACE, provider=provider)
    second = await billing_service.sync_site_addons(WORKSPACE, provider=provider)

    assert first["addons"] == second["addons"] == [{"addon_id": "adn_site", "quantity": 1}]
