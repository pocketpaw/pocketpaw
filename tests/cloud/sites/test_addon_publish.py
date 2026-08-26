# tests/cloud/sites/test_addon_publish.py — the ADD-ON PUBLISH RAIL end to end:
# buying a paid site attaches a LINE to the workspace's existing subscription and
# deploys in the same request, instead of opening a per-site checkout.
#
# What separates this from tests/cloud/billing/test_site_plan_addons.py: that one
# proves the cart and the sync in isolation, this one drives the real
# ``publish_pocket`` dispatcher and asserts on what the buyer actually gets — a
# live site and no redirect — and on what they must NOT get, a second
# subscription.
#
# The three that are worth the most:
#
#   * ``test_a_paid_publish_deploys_in_the_same_request`` — there is no pending
#     state and no ``checkout_url`` on this rail. The charge is synchronous, so
#     the deploy can be too.
#   * ``test_upgrading_a_live_free_site_ships_the_new_content`` — the regression
#     that the obvious implementation gets wrong. ``activate_site``'s idempotency
#     guard returns early for a site that is already deployed+active, and this
#     rail marks the site active BEFORE charging (the cart is built from the
#     documents). Without ``force=True`` the customer is charged and keeps seeing
#     the old site, with every status field claiming success.
#   * ``test_a_declined_charge_leaves_nothing_paid_for`` — a refusal must not
#     leave the site holding paid capabilities. Nothing else rewrites
#     ``subscription_status``, so a missed revert is permanent.
#
# Created 2026-08-26 (feat/site-plans-as-addons): new test module.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.errors import NoActiveSubscription
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.subscription import Subscription
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.anyio

WS_SUB_ID = "sub_workspace_real"


class _RecordingGenerator:
    def __init__(self):
        self.build_calls: list[dict] = []

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.build_calls.append(dict(kw))
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _RecordingBillingProvider:
    """Records what the publish asked the gateway to do.

    ``create_subscription`` is present and recorded precisely so a test can prove
    it was NOT called — the whole point of this rail is that no second
    subscription is ever opened.
    """

    def __init__(self, *, change_plan_raises: Exception | None = None):
        self.create_calls: list[dict] = []
        self.change_plan_calls: list[dict] = []
        self._change_plan_raises = change_plan_raises

    async def create_subscription(self, **kw):
        from pocketpaw_ee.cloud.billing.domain import SubscriptionCheckout

        self.create_calls.append(dict(kw))
        return SubscriptionCheckout(checkout_url="https://nope.test", subscription_id="cks_nope")

    async def change_plan(self, *, subscription_id, product_id, plan_key, addons):
        if self._change_plan_raises is not None:
            raise self._change_plan_raises
        self.change_plan_calls.append(
            {
                "subscription_id": subscription_id,
                "product_id": product_id,
                "plan_key": plan_key,
                "addons": addons,
            }
        )

    async def cancel_subscription(self, subscription_id: str) -> None:  # pragma: no cover
        raise AssertionError("the add-on rail must never cancel a subscription")


async def _make_workspace(plan: str = "pro") -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme",
        slug=f"acme-addon-{datetime.now(UTC).timestamp()}",
        owner="u1",
        plan=plan,
    )
    await ws.insert()
    return str(ws.id)


async def _make_pocket(*, workspace_id: str, owner: str = "u1") -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id,
        name="My Landing",
        owner=owner,
        type="site",
        pattern="landing",
    )
    await doc.insert()
    return str(doc.id)


async def _make_subscription(workspace_id: str) -> None:
    doc = Subscription(
        workspace=workspace_id,
        gateway="dodo",
        gateway_subscription_id=WS_SUB_ID,
        plan_key="pro",
        product_id="prod_ws_pro",
        status="active",
    )
    await doc.insert()


def _addon_rail_only(monkeypatch) -> None:
    """Configure the ADD-ON map and leave the per-site product map empty.

    The two together are the intended end state; separating them here means a
    test that asserts "no second subscription" is proving the dispatcher chose
    the add-on rail rather than proving the old rail happened to be unconfigured.
    """
    monkeypatch.setattr(
        site_plans,
        "_dodo_addon_for",
        lambda key: {"site": "adn_site", "staff": "adn_staff"}.get(key),
    )
    monkeypatch.setattr(site_plans, "_dodo_product_for", lambda key: None)


def _local_deploy(monkeypatch) -> list[str]:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    gen = _RecordingGenerator()
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: gen)
    from pocketpaw_ee.sites import local_server

    deploys: list[str] = []

    def _fake_deploy_local(site_id, project_dir, **kw):
        deploys.append(site_id)
        return f"http://local/{site_id}/"

    monkeypatch.setattr(local_server, "deploy_local", _fake_deploy_local)
    return deploys


# --------------------------------------------------------------------------- #


async def test_a_paid_publish_deploys_in_the_same_request(mongo_db, monkeypatch):  # noqa: ARG001
    """No pending state, no checkout_url, no second subscription.

    The old rail had to defer the deploy because payment happened on a hosted
    checkout the buyer wandered off to. Attaching an add-on charges the card on
    the subscription synchronously, so the answer to "did they pay" is known
    inside this request and the site can go live in it."""
    _addon_rail_only(monkeypatch)
    deploys = _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert provider.create_calls == [], (
        "a paid site must not open a subscription of its own — that is the "
        "separate payment this rail replaces"
    )
    call = provider.change_plan_calls[0]
    assert call["subscription_id"] == WS_SUB_ID
    assert call["addons"] == [{"addon_id": "adn_site", "quantity": 1}]

    assert doc.deployed is True
    assert doc.subscription_status == "active"
    assert doc.plan_tier == "site"
    assert deploys, "the site must actually deploy in this request"

    assert doc.subscription_id is None, (
        "no PER-SITE subscription exists, and the field is the cart builder's "
        "discriminator — stamping the workspace's id here would drop the site "
        "off its own invoice"
    )
    assert sites_service._to_response(doc).checkout_url is None, (
        "the buyer never leaves the app on this rail"
    )


async def test_upgrading_a_live_free_site_ships_the_new_content(mongo_db, monkeypatch):  # noqa: ARG001
    """THE REGRESSION THIS RAIL INVITES. The site is marked active BEFORE the
    charge, because the cart is built from the documents. ``activate_site``
    treats "deployed and active" as terminal and returns early — so an upgrade of
    an already-live free site would charge the customer and redeploy nothing,
    while every status field reported success."""
    _addon_rail_only(monkeypatch)
    deploys = _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    # First publish on the FREE floor — goes live, no charge.
    free_doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="free",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )
    assert free_doc.deployed is True
    assert provider.change_plan_calls == []
    deploys.clear()

    # Now buy a paid tier for the SAME pocket.
    paid_doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert provider.change_plan_calls, "the upgrade must actually charge"
    assert deploys, (
        "the customer paid for an upgrade and got no redeploy — activate_site's "
        "idempotency guard swallowed it (this is what force=True exists for)"
    )
    assert paid_doc.plan_tier == "site"
    assert paid_doc.subscription_status == "active"
    assert paid_doc.deployed is True


async def test_a_declined_charge_leaves_nothing_paid_for(mongo_db, monkeypatch):  # noqa: ARG001
    """A refusal propagates, and the site must not keep the paid status it was
    optimistically given. Nothing else ever rewrites that field, so a missed
    revert hands out badge removal, custom domains and the concierge for free and
    for good."""
    _addon_rail_only(monkeypatch)
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider(change_plan_raises=RuntimeError("card declined"))

    with pytest.raises(RuntimeError, match="card declined"):
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="site",
            _bundle_reader=lambda d: b"x",
            _billing_provider=provider,
        )

    doc = await Site.find_one(Site.workspace == ws)
    assert doc is not None
    assert doc.subscription_status != "active", (
        "a declined card must not leave the site holding every paid capability"
    )
    assert doc.deployed is False, "nothing was paid for, so nothing goes live"


async def test_a_workspace_with_no_subscription_cannot_buy_a_site(mongo_db, monkeypatch):  # noqa: ARG001
    """The deliberate funnel consequence, asserted so it is a decision rather than
    an accident: an add-on attaches to something. A free workspace has to start a
    workspace subscription first — the alternative is opening the standalone
    per-site subscription this rail exists to remove."""
    _addon_rail_only(monkeypatch)
    _local_deploy(monkeypatch)
    ws = await _make_workspace()  # deliberately NO subscription
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    with pytest.raises(NoActiveSubscription):
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="site",
            _bundle_reader=lambda d: b"x",
            _billing_provider=provider,
        )

    assert provider.create_calls == [], (
        "refusing must not fall back to opening a per-site subscription"
    )


async def test_the_legacy_per_site_rail_still_runs_when_no_addon_is_configured(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Grandfathering. Per-site subscriptions are live in production; a tier with a
    product and no add-on must still take the old charge-first path so a
    part-configured deployment keeps selling."""
    monkeypatch.setattr(site_plans, "_dodo_addon_for", lambda key: None)
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"site": "prod_site_pro"}.get(key)
    )
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert provider.create_calls, "the legacy rail opens a per-site checkout"
    assert provider.change_plan_calls == []
    assert doc.subscription_status == "pending"
    assert doc.deployed is False


async def test_republishing_a_paid_addon_site_does_not_charge_again(mongo_db, monkeypatch):  # noqa: ARG001
    """A republish is a CONTENT EDIT, not a purchase.

    An add-on site deliberately holds no per-site ``subscription_id`` — that
    absence is how the cart builder tells the two rails apart — so the old
    "already paying means it has a subscription id" test read every add-on site as
    unpaid and re-ran the charge on every content edit."""
    _addon_rail_only(monkeypatch)
    deploys = _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )
    assert len(provider.change_plan_calls) == 1
    deploys.clear()

    # Same pocket, same tier, new content.
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert len(provider.change_plan_calls) == 1, (
        "a content edit must not touch billing — the declarative cart makes a "
        "re-charge idempotent, but churning proration on every publish is not"
    )
    assert provider.create_calls == []
    assert doc.subscription_status == "active"
    assert doc.plan_tier == "site"
    assert deploys, "the new content still has to go live"


async def test_a_tier_change_on_the_addon_rail_resyncs_the_cart(mongo_db, monkeypatch):  # noqa: ARG001
    """There is no per-site subscription to move, so an upgrade is expressed as a
    different cart on the workspace's subscription."""
    _addon_rail_only(monkeypatch)
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert doc.plan_tier == "staff"
    assert provider.create_calls == []
    assert provider.change_plan_calls[-1]["addons"] == [{"addon_id": "adn_staff", "quantity": 1}], (
        "the site moved rungs, so the cart carries the new tier and NOT the old "
        "one — a cart that kept both would bill the customer for two sites"
    )


async def test_both_rails_configured_still_never_opens_a_per_site_subscription(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """THE ROLLOUT STATE, and the one where getting this wrong costs money.

    A deployment mid-migration has the per-site product map AND the add-on map
    set. Every code path that can reach ``create_subscription`` has to check the
    add-on rail first, or the customer is billed on both at once."""
    monkeypatch.setattr(site_plans, "_dodo_addon_for", lambda key: {"site": "adn_site"}.get(key))
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"site": "prod_site_pro"}.get(key)
    )
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert provider.create_calls == [], (
        "both maps configured is the normal rollout state — the add-on rail must "
        "win, or the site is billed twice for one purchase"
    )
    assert provider.change_plan_calls
    assert doc.subscription_id is None
    assert doc.deployed is True


async def test_the_stamp_path_refuses_to_open_a_per_site_sub_for_an_addon_tier(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Defence in depth on ``_apply_site_plan``, exercised directly.

    The dispatcher already routes an add-on tier away from this function, so
    today nothing reaches it with both rails configured — which is exactly why
    this is called directly rather than through ``publish_pocket``. The guard
    protects a BILLING path against a future refactor that changes the routing,
    and an untested guard in a billing path is a guess. Reaching in is the honest
    way to prove it, and the mutation plan confirms it fails without the check."""
    monkeypatch.setattr(site_plans, "_dodo_addon_for", lambda key: {"site": "adn_site"}.get(key))
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"site": "prod_site_pro"}.get(key)
    )
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    doc = Site(
        workspace=ws,
        pocket_id=pocket_id,
        owner="u1",
        name="direct",
        plan_tier=None,
        subscription_status="none",
    )
    await doc.insert()

    await sites_service._apply_site_plan(
        doc=doc,
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        provider=provider,
    )

    assert provider.create_calls == [], (
        "the tier has an add-on rail, so this path must never open a per-site "
        "subscription for it — that is a second charge for one site"
    )
