# tests/cloud/sites/test_site_plan_purchase_authz.py — WHO may commit the
# workspace to a recurring site charge.
#
# Created 2026-09-01 (fix/sites-plan-purchase-authz). The hole: publishing a site
# needs ``fabric.write``, which is MEMBER, and nothing anywhere asked a second
# question about the TIER. Once site plans became add-on lines on the workspace's
# own subscription, a paid publish charged the company card inside the request —
# so the most junior role in the workspace could commit it to a recurring charge
# while being too junior to even VIEW the billing page (``billing.view`` is
# ADMIN). Before add-ons this was merely rude (the employee was redirected to a
# checkout and asked to pay personally); afterwards it was unauthorized spend.
#
# What these pin, and why each is a way to get it wrong:
#
#   * Publishing FREE stays a MEMBER action. Gating the whole publish would make
#     every employee's ordinary work need an admin, which is not the ask.
#   * A REPUBLISH of a site already on a paid tier is a content edit, not a
#     purchase. Refusing it would break the daily workflow of the person who
#     builds the site for a plan someone else already bought.
#   * The service refuses by DEFAULT. The gate cannot live only in the router:
#     ``publish_pocket`` is shared with the in-process MCP publish tool, which
#     passes no tier today and could start to. ``purchase_authorized`` defaults
#     False so a new caller fails closed rather than silently buying.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.anyio


class _RecordingBillingProvider:
    def __init__(self):
        self.create_calls: list[dict] = []
        self.change_plan_calls: list[dict] = []

    async def create_subscription(self, **kw):
        from pocketpaw_ee.cloud.billing.domain import SubscriptionCheckout

        self.create_calls.append(dict(kw))
        return SubscriptionCheckout(checkout_url="https://nope.test", subscription_id="cks_x")

    async def change_plan(self, *, subscription_id, product_id, plan_key, addons):
        self.change_plan_calls.append({"plan_key": plan_key, "addons": addons})

    async def cancel_subscription(self, subscription_id: str) -> None:  # pragma: no cover
        raise AssertionError("not expected")


class _RecordingGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


async def _make_workspace() -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme", slug=f"acme-authz-{datetime.now(UTC).timestamp()}", owner="u1", plan="pro"
    )
    await ws.insert()
    return str(ws.id)


async def _make_pocket(workspace_id: str) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id, name="Landing", owner="u1", type="site", pattern="landing"
    )
    await doc.insert()
    return str(doc.id)


async def _make_subscription(workspace_id: str) -> None:
    from pocketpaw_ee.cloud.models.subscription import Subscription

    await Subscription(
        workspace=workspace_id,
        gateway="dodo",
        gateway_subscription_id="sub_ws",
        plan_key="pro",
        product_id="prod_ws_pro",
        status="active",
    ).insert()


def _addon_rail(monkeypatch) -> None:
    monkeypatch.setattr(
        site_plans, "_dodo_addon_for", lambda k: {"site": "adn_site", "staff": "adn_staff"}.get(k)
    )
    monkeypatch.setattr(site_plans, "_dodo_product_for", lambda k: None)


def _local_deploy(monkeypatch) -> None:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: _RecordingGenerator())
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(local_server, "deploy_local", lambda sid, d, **kw: f"http://local/{sid}/")


# --------------------------------------------------------------------------- #


async def test_an_unauthorized_caller_cannot_buy_a_paid_tier(mongo_db, monkeypatch):  # noqa: ARG001
    """THE HOLE. A member asking for a $19 tier must not charge the company."""
    _addon_rail(monkeypatch)
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(ws)
    provider = _RecordingBillingProvider()

    with pytest.raises(Forbidden) as exc:
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="staff",
            _bundle_reader=lambda d: b"x",
            _billing_provider=provider,
        )

    assert exc.value.code == "sites.plan_purchase_forbidden"
    assert provider.change_plan_calls == [], "nothing may reach the gateway"
    assert provider.create_calls == []


async def test_the_default_is_refusal(mongo_db, monkeypatch):  # noqa: ARG001
    """``purchase_authorized`` defaults False, so a caller that never heard of it
    fails closed. The MCP publish tool shares this function and passes no tier
    today; if it ever starts to, it must not be able to buy by omission."""
    import inspect

    sig = inspect.signature(sites_service.publish_pocket)
    assert sig.parameters["purchase_authorized"].default is False


async def test_an_authorized_caller_buys_normally(mongo_db, monkeypatch):  # noqa: ARG001
    _addon_rail(monkeypatch)
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(ws)
    provider = _RecordingBillingProvider()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert doc.plan_tier == "staff"
    assert provider.change_plan_calls, "an authorized buy still charges"


async def test_a_free_publish_needs_no_authorization(mongo_db, monkeypatch):  # noqa: ARG001
    """The ordinary employee workflow. Gating this would make building a site
    need an admin, which is not what the hole was about."""
    _addon_rail(monkeypatch)
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(ws)
    provider = _RecordingBillingProvider()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="free",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert doc.deployed is True
    assert provider.change_plan_calls == []


async def test_republishing_a_paid_site_needs_no_authorization(mongo_db, monkeypatch):  # noqa: ARG001
    """A content edit on a site someone already bought. The member who builds the
    site must be able to ship changes without an admin present each time."""
    _addon_rail(monkeypatch)
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(ws)
    provider = _RecordingBillingProvider()

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    # Same tier, no authorization — a republish, not a purchase.
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert doc.plan_tier == "staff"


async def test_an_unauthorized_upgrade_is_refused(mongo_db, monkeypatch):  # noqa: ARG001
    """Moving a site UP a rung is a purchase too. Only same-tier republishes are
    exempt — otherwise a member upgrades $7 to $19 and calls it an edit."""
    _addon_rail(monkeypatch)
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    pocket_id = await _make_pocket(ws)
    provider = _RecordingBillingProvider()

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )
    before = len(provider.change_plan_calls)

    with pytest.raises(Forbidden):
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="staff",
            _bundle_reader=lambda d: b"x",
            _billing_provider=provider,
        )

    assert len(provider.change_plan_calls) == before


def test_buying_sits_above_publishing_in_the_role_ladder():
    """The two questions must not collapse back into one. Publishing is MEMBER;
    buying is higher, and higher than nothing is the whole point."""
    from pocketpaw_ee.guards.actions import ACTIONS
    from pocketpaw_ee.guards.rbac import WorkspaceRole

    assert ACTIONS["fabric.write"].minimum == WorkspaceRole.MEMBER
    assert ACTIONS["sites.buy_plan"].minimum.level > WorkspaceRole.MEMBER.level
    # And no higher than the tier that can read the bill it adds to.
    assert ACTIONS["sites.buy_plan"].minimum.level <= ACTIONS["billing.view"].minimum.level


# --------------------------------------------------------------------------- #
# The router's answer — the one that actually decides in production.
#
# Every test above drives ``publish_pocket`` directly, which proves the service
# refuses an unauthorized buy but says nothing about whether the router ever
# reports someone as unauthorized. A helper stuck at True would leave the HTTP
# endpoint wide open with the whole suite green; the mutation plan caught exactly
# that, which is why these exist.
# --------------------------------------------------------------------------- #


def _user(role: str, workspace_id: str = "ws-1", user_id: str = "u-1"):
    return SimpleNamespace(
        id=user_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )


def test_the_router_refuses_a_member_and_allows_an_admin():
    from pocketpaw_ee.sites.router import _may_buy_site_plan

    assert _may_buy_site_plan(_user("member"), "ws-1") is False
    assert _may_buy_site_plan(_user("editor"), "ws-1") is False
    assert _may_buy_site_plan(_user("admin"), "ws-1") is True
    assert _may_buy_site_plan(_user("owner"), "ws-1") is True


def test_the_router_refuses_a_non_member_and_a_broken_user():
    """Fail closed on anything it cannot read a role from. A predicate that
    answers True when it does not know is a predicate that sells a plan to a
    stranger."""
    from pocketpaw_ee.sites.router import _may_buy_site_plan

    assert _may_buy_site_plan(_user("admin", workspace_id="someone-else"), "ws-1") is False
    assert _may_buy_site_plan(object(), "ws-1") is False
    assert _may_buy_site_plan(None, "ws-1") is False
