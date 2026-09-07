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
#
# Amended 2026-09-05 (fix/sites-plan-credits). Dropping a paying site to the free
# floor used to leave its subscription open and change nothing anyone was billed
# for, so the gate only had to ask "is the tier being BOUGHT a priced one?". The
# credits rail closes the subscription on that move, which makes it a billing
# change a member could perform — see
# ``test_a_member_cannot_cancel_a_paid_plan_by_applying_free``.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden
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


# THE WORKSPACE PLAN MATTERS NOW, and this fixture pins it deliberately.
#
# Two facts collide (2026-09-06, feat/plan-included-sites): Paw Go / Pro / Pro Max
# CARRY sites — 1 / 3 / 10, at ``staff`` quality for no money — and the ``sites``
# feature itself starts at Go, so a ``free`` workspace cannot publish one at all.
# Between them the credit wallet is reached in exactly one situation: a workspace
# that has used up the sites its plan carries. So the workspace here is on ``go``
# with its ONE slot already filled. That is the only population this rail has
# left, and a fixture that skipped it would test a path no customer can reach.
#
# It matters twice over here: the authorization gate EXEMPTS a move the plan
# carries (taking a slot spends nothing, so it needs no admin), which is its own
# test below. Every gate test in this module is therefore about the OVERFLOW
# purchase — the one that really does commit the company to a charge.
async def _make_workspace() -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme", slug=f"acme-authz-{datetime.now(UTC).timestamp()}", owner="u1", plan="go"
    )
    await ws.insert()
    await _fill_plan_slots(str(ws.id))
    return str(ws.id)


async def _fill_plan_slots(workspace_id: str, count: int = 1) -> None:
    """Occupy ``count`` of the workspace's plan-carried site slots.

    Decoy sites on their own pocket ids, so they never collide with the site under
    test. Stamped exactly as a carried site is — plan rail, staff tier, active, no
    renewal date — because ``plan_site_slots`` counts on ``billing_rail`` and a
    decoy with the wrong shape leaves the slot open and sends the test down the
    free rail while looking like it did the opposite."""
    from pocketpaw_ee.cloud.models.site import Site as _S

    for i in range(count):
        await _S(
            workspace=workspace_id,
            pocket_id=f"decoy-{workspace_id}-{i}",
            owner="u1",
            name=f"Decoy {i}",
            deployed=True,
            url="http://local/decoy/",
            plan_tier="staff",
            subscription_status="active",
            billing_rail="plan",
        ).insert()


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


def _local_deploy(monkeypatch) -> None:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: _RecordingGenerator())
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(local_server, "deploy_local", lambda sid, d, **kw: f"http://local/{sid}/")


async def _balance(workspace_id: str) -> int:
    from pocketpaw_ee.cloud.credits import service as credits_service

    return await credits_service.balance(workspace_id)


async def _fund(workspace_id: str, credits: int = 9000) -> None:
    """Put credits in the wallet, because a paid site is bought from it since
    2026-09-05.

    Called by the REFUSAL cases too, and deliberately. The authorization gate runs
    before any charge, so an empty wallet would also stop the purchase — with a
    ``402`` rather than the ``Forbidden`` these tests name. Funding first means the
    refusal they assert can only be the gate's."""
    from pocketpaw_ee.cloud.credits import service as credits_service

    await credits_service.grant(
        workspace=workspace_id,
        amount=credits,
        cause="top_up",
        idempotency_key=f"seed-{workspace_id}-{credits}",
    )


# --------------------------------------------------------------------------- #


async def test_an_unauthorized_caller_cannot_buy_a_paid_tier(mongo_db, monkeypatch):  # noqa: ARG001
    """THE HOLE. A member asking for a $19 tier must not charge the company."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    with pytest.raises(Forbidden) as exc:
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="staff",
            _bundle_reader=lambda d: b"x",
        )

    assert exc.value.code == "sites.plan_purchase_forbidden"
    # And no money moved. The wallet is funded, so a balance still at its seed
    # value is the refusal biting rather than an empty balance masking it.
    assert await _balance(ws) == 9000


async def test_the_default_is_refusal(mongo_db, monkeypatch):  # noqa: ARG001
    """``purchase_authorized`` defaults False, so a caller that never heard of it
    fails closed. The MCP publish tool shares this function and passes no tier
    today; if it ever starts to, it must not be able to buy by omission."""
    import inspect

    sig = inspect.signature(sites_service.publish_pocket)
    assert sig.parameters["purchase_authorized"].default is False


async def test_an_authorized_caller_buys_normally(mongo_db, monkeypatch):  # noqa: ARG001
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )

    assert doc.plan_tier == "staff"
    # An authorized buy still CHARGES — it just charges the wallet rather than the
    # gateway. Asserted on the balance, because "the gateway was called" stopped
    # being the signal that money moved when the rail changed, and a test that
    # only checked the tier landed would pass on a purchase nobody paid for.
    from pocketpaw_ee.cloud.credits import service as credits_service

    assert await credits_service.balance(ws) == 9000 - 1900


async def test_a_free_publish_needs_no_authorization(mongo_db, monkeypatch):  # noqa: ARG001
    """The ordinary employee workflow. Gating this would make building a site
    need an admin, which is not what the hole was about."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="free",
        _bundle_reader=lambda d: b"x",
    )

    assert doc.deployed is True
    # Unfunded on purpose: the floor must publish with an EMPTY wallet, which is
    # the state of every workspace that has not bought credits yet. A seeded
    # balance here would pass even if the free publish had started charging.
    assert await _balance(ws) == 0, "the free floor costs nothing"


async def test_republishing_a_paid_site_needs_no_authorization(mongo_db, monkeypatch):  # noqa: ARG001
    """A content edit on a site someone already bought. The member who builds the
    site must be able to ship changes without an admin present each time."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )

    # Same tier, no authorization — a republish, not a purchase.
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        _bundle_reader=lambda d: b"x",
    )

    assert doc.plan_tier == "staff"


async def test_an_unauthorized_upgrade_is_refused(mongo_db, monkeypatch):  # noqa: ARG001
    """Moving a site UP a rung is a purchase too. Only same-tier republishes are
    exempt — otherwise a member upgrades $7 to $19 and calls it an edit."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )
    before = await _balance(ws)

    with pytest.raises(Forbidden):
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="staff",
            _bundle_reader=lambda d: b"x",
        )

    assert await _balance(ws) == before, "a refused upgrade must not charge"


async def test_a_member_cannot_cancel_a_paid_plan_by_applying_free(mongo_db, monkeypatch):  # noqa: ARG001
    """THE OTHER DIRECTION, and it opened the day the free path started closing
    the subscription. Publishing ``free`` costs nothing, so a gate that asks only
    "is the requested tier priced?" waves it through — and it ENDS a plan the
    workspace is paying for, taking the custom domain, the concierge and badge
    removal off a live site. Nothing refunds the month already bought and nothing
    restores the tier; the site simply drops. Spending the company's money and
    destroying what it already bought are the same decision seen from two sides.

    Mutation: narrow the gate back to ``is_paid`` alone and this is the only test
    that fails."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )
    renewal_before = doc.renewal_date
    assert renewal_before is not None

    with pytest.raises(Forbidden) as exc:
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u2",
            pocket_id=pocket_id,
            site_plan_key="free",
            _bundle_reader=lambda d: b"x",
        )

    assert exc.value.code == "sites.plan_purchase_forbidden"

    # Re-read rather than trusting the in-memory doc: the refusal has to have
    # happened BEFORE any write, so the row the next renewal sweep reads is the
    # one the admin paid for.
    from pocketpaw_ee.cloud.models.site import Site

    fresh = await Site.get(doc.id)
    assert fresh.plan_tier == "staff"
    assert fresh.subscription_status == "active"
    assert fresh.renewal_date == renewal_before
    assert fresh.period_paid_usd == 19


async def test_an_admin_may_still_cancel_a_paid_plan(mongo_db, monkeypatch):  # noqa: ARG001
    """The gate above must not lock the door on the person holding the key. An
    admin dropping a site to free is the supported way to stop paying for it, and
    a refusal here would leave the only exit as a support ticket.

    What cancelling DOES — schedule the close for the end of the paid period
    rather than take the month back — is pinned in ``test_credits_tier_changes``.
    This one is only about who may ask."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _make_subscription(ws)
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="staff",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )
    after_purchase = await _balance(ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="free",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )

    assert doc.plan_cancels_at_period_end is True
    # Cancelling is not a purchase. A charge here would bill somebody for leaving.
    assert await _balance(ws) == after_purchase


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
